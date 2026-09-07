/**
 * ChartDrawingLayer — a free, TradingView-style drawing toolbox that rides on
 * top of the existing lightweight-charts pane.
 *
 * It draws onto an absolutely-positioned canvas over the chart and converts
 * every anchor to {time, price} so shapes pan and zoom with the candles. Tools:
 * pan (default — chart stays fully interactive), select/move, trendline, ray,
 * horizontal line, rectangle, fib retracement, text, and eraser. Fullscreen via
 * the Fullscreen API. Every change auto-saves (debounced) to the Kronos plugin
 * so the drawings survive reloads AND become context the trading-room agents
 * read — the "Refine" button asks the agents to snap the shapes onto real swing
 * structure and merges their sharper copies back in.
 *
 * Fully self-contained and opt-in: TradingViewChart mounts it only when
 * `enableDrawings` is set, so every other chart on the app is untouched.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { IChartApi, ISeriesApi, Time } from 'lightweight-charts';
import {
  MousePointer2, Move, Minus, Slash, Square, TrendingUp, Type as TypeIcon,
  Eraser, Trash2, Sparkles, Maximize2, Minimize2, Check, Loader2,
} from 'lucide-react';
import { apiClient } from '@/services/api';

type ToolId =
  | 'pan' | 'select' | 'trendline' | 'ray' | 'hline' | 'rectangle' | 'fib' | 'text' | 'eraser';

interface Pt { time: number; price: number }
interface Drawing {
  id: string;
  type: 'trendline' | 'ray' | 'hline' | 'vline' | 'rectangle' | 'fib' | 'text';
  points: Pt[];
  color: string;
  width: number;
  text?: string | null;
  source: 'user' | 'agent';
  locked?: boolean;
  agent_note?: string | null;
  refined_from?: string | null;
}

interface Props {
  chartApiRef: React.MutableRefObject<IChartApi | null>;
  seriesApiRef: React.MutableRefObject<ISeriesApi<'Candlestick'> | null>;
  containerRef: React.RefObject<HTMLDivElement | null>;
  chartInitCount: number;          // rebind when the chart is recreated
  exchange: string;
  symbol: string;
  timeframe: string;               // API timeframe, e.g. '1h'
  onToggleFullscreen: () => void;
  isFullscreen: boolean;
}

const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const SWATCHES = ['#22d3ee', '#f5c451', '#ef4444', '#22c55e', '#e5e7eb', '#a855f7'];
const genId = () => `usr_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;

const TOOLS: { id: ToolId; icon: any; label: string }[] = [
  { id: 'pan', icon: MousePointer2, label: 'Pan / zoom (chart)' },
  { id: 'select', icon: Move, label: 'Select & move' },
  { id: 'trendline', icon: TrendingUp, label: 'Trend line' },
  { id: 'ray', icon: Slash, label: 'Ray (extends right)' },
  { id: 'hline', icon: Minus, label: 'Horizontal level' },
  { id: 'rectangle', icon: Square, label: 'Rectangle / zone' },
  { id: 'fib', icon: TrendingUp, label: 'Fib retracement' },
  { id: 'text', icon: TypeIcon, label: 'Text note' },
  { id: 'eraser', icon: Eraser, label: 'Erase (click a shape)' },
];

// Normalise a lightweight-charts Time to unix seconds (handles BusinessDay).
function timeToNum(t: Time | number | null | undefined): number | null {
  if (t == null) return null;
  if (typeof t === 'number') return t;
  if (typeof t === 'object' && 'year' in (t as any)) {
    const d = t as any;
    return Math.floor(Date.UTC(d.year, d.month - 1, d.day) / 1000);
  }
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

export default function ChartDrawingLayer({
  chartApiRef, seriesApiRef, containerRef, chartInitCount,
  exchange, symbol, timeframe, onToggleFullscreen, isFullscreen,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [tool, setTool] = useState<ToolId>('pan');
  const [color, setColor] = useState<string>('#22d3ee');
  const [drawings, setDrawings] = useState<Drawing[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved'>('idle');
  const [refining, setRefining] = useState(false);
  const [agentNote, setAgentNote] = useState<string | null>(null);
  const [portalReady, setPortalReady] = useState(false);
  useEffect(() => { setPortalReady(true); }, []);

  // Refs mirror state so the imperative repaint always reads the latest.
  const drawingsRef = useRef<Drawing[]>([]);
  const selectedRef = useRef<string | null>(null);
  const toolRef = useRef<ToolId>('pan');
  drawingsRef.current = drawings;
  selectedRef.current = selectedId;
  toolRef.current = tool;

  const draftRef = useRef<Drawing | null>(null);      // shape being drawn
  const dragRef = useRef<{ id: string; last: Pt } | null>(null); // shape being moved
  const suppressSaveRef = useRef(false);              // skip autosave after load/refine

  // ── Coordinate conversion ──────────────────────────────────────────────────
  const priceToY = useCallback((price: number): number | null => {
    const y = seriesApiRef.current?.priceToCoordinate(price);
    return y == null ? null : y;
  }, [seriesApiRef]);

  const yToPrice = useCallback((y: number): number | null => {
    const p = seriesApiRef.current?.coordinateToPrice(y);
    return p == null ? null : (p as number);
  }, [seriesApiRef]);

  const timeToX = useCallback((time: number): number | null => {
    const ts = chartApiRef.current?.timeScale();
    if (!ts) return null;
    const direct = ts.timeToCoordinate(time as Time);
    if (direct != null) return direct;
    // Off-screen anchor: extrapolate from the visible range's linear time→x map.
    try {
      const vr = ts.getVisibleRange();
      if (!vr) return null;
      const from = timeToNum(vr.from as Time);
      const to = timeToNum(vr.to as Time);
      if (from == null || to == null || to === from) return null;
      const xFrom = ts.timeToCoordinate(vr.from);
      const xTo = ts.timeToCoordinate(vr.to);
      if (xFrom == null || xTo == null) return null;
      return xFrom + ((time - from) / (to - from)) * (xTo - xFrom);
    } catch { return null; }
  }, [chartApiRef]);

  const xToTime = useCallback((x: number): number | null => {
    const ts = chartApiRef.current?.timeScale();
    if (!ts) return null;
    const direct = timeToNum(ts.coordinateToTime(x));
    if (direct != null) return direct;
    try {
      const vr = ts.getVisibleRange();
      if (!vr) return null;
      const from = timeToNum(vr.from as Time);
      const to = timeToNum(vr.to as Time);
      if (from == null || to == null) return null;
      const xFrom = ts.timeToCoordinate(vr.from);
      const xTo = ts.timeToCoordinate(vr.to);
      if (xFrom == null || xTo == null || xTo === xFrom) return null;
      return from + ((x - xFrom) / (xTo - xFrom)) * (to - from);
    } catch { return null; }
  }, [chartApiRef]);

  // ── Rendering ───────────────────────────────────────────────────────────────
  const priceFmt = useCallback((p: number) => {
    const a = Math.abs(p);
    const d = a >= 1000 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 8;
    return p.toFixed(d);
  }, []);

  const repaint = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const w = container.clientWidth;
    const h = container.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const drawOne = (d: Drawing, isDraft = false) => {
      const selected = d.id === selectedRef.current;
      ctx.save();
      ctx.strokeStyle = d.color;
      ctx.fillStyle = d.color;
      ctx.lineWidth = d.width || 2;
      if (d.source === 'agent') ctx.setLineDash([6, 4]);
      const pts = d.points;

      if (d.type === 'hline' && pts[0]) {
        const y = priceToY(pts[0].price);
        if (y == null) { ctx.restore(); return; }
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = '11px ui-sans-serif, system-ui';
        ctx.fillText(`${priceFmt(pts[0].price)}${d.source === 'agent' ? ' ✦' : ''}`, 6, y - 4);
        if (selected) { ctx.fillRect(w / 2 - 3, y - 3, 6, 6); }
      } else if ((d.type === 'trendline' || d.type === 'ray') && pts.length >= 2) {
        const x1 = timeToX(pts[0].time), y1 = priceToY(pts[0].price);
        let x2 = timeToX(pts[1].time), y2 = priceToY(pts[1].price);
        if (x1 == null || y1 == null || x2 == null || y2 == null) { ctx.restore(); return; }
        if (d.type === 'ray' && x2 !== x1) {
          // Extend to the right edge along the same slope.
          const slope = (y2 - y1) / (x2 - x1);
          if (x2 >= x1) { y2 = y1 + slope * (w - x1); x2 = w; }
        }
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
        if (selected) { ctx.fillRect(x1 - 3, y1 - 3, 6, 6); ctx.fillRect(x2 - 3, y2 - 3, 6, 6); }
      } else if (d.type === 'rectangle' && pts.length >= 2) {
        const x1 = timeToX(pts[0].time), y1 = priceToY(pts[0].price);
        const x2 = timeToX(pts[1].time), y2 = priceToY(pts[1].price);
        if (x1 == null || y1 == null || x2 == null || y2 == null) { ctx.restore(); return; }
        const rx = Math.min(x1, x2), ry = Math.min(y1, y2);
        const rw = Math.abs(x2 - x1), rh = Math.abs(y2 - y1);
        ctx.globalAlpha = 0.12; ctx.fillRect(rx, ry, rw, rh); ctx.globalAlpha = 1;
        ctx.strokeRect(rx, ry, rw, rh);
        if (selected) { ctx.fillRect(x1 - 3, y1 - 3, 6, 6); ctx.fillRect(x2 - 3, y2 - 3, 6, 6); }
      } else if (d.type === 'fib' && pts.length >= 2) {
        const x1 = timeToX(pts[0].time);
        const yHi = priceToY(Math.max(pts[0].price, pts[1].price));
        const yLo = priceToY(Math.min(pts[0].price, pts[1].price));
        const pHi = Math.max(pts[0].price, pts[1].price);
        const pLo = Math.min(pts[0].price, pts[1].price);
        if (x1 == null || yHi == null || yLo == null) { ctx.restore(); return; }
        ctx.setLineDash([]);
        FIB_LEVELS.forEach((lv) => {
          const price = pHi - (pHi - pLo) * lv;
          const y = priceToY(price);
          if (y == null) return;
          ctx.globalAlpha = 0.9;
          ctx.beginPath(); ctx.moveTo(Math.max(0, x1), y); ctx.lineTo(w, y); ctx.stroke();
          ctx.font = '10px ui-sans-serif, system-ui';
          ctx.fillText(`${lv} · ${priceFmt(price)}`, w - 120, y - 3);
        });
        ctx.globalAlpha = 1;
        if (selected) { ctx.fillRect((x1 ?? 0) - 3, yHi - 3, 6, 6); ctx.fillRect((x1 ?? 0) - 3, yLo - 3, 6, 6); }
      } else if (d.type === 'text' && pts[0]) {
        const x = timeToX(pts[0].time), y = priceToY(pts[0].price);
        if (x == null || y == null) { ctx.restore(); return; }
        ctx.setLineDash([]);
        ctx.font = '13px ui-sans-serif, system-ui';
        ctx.fillText(d.text || 'note', x, y);
        if (selected) { ctx.strokeRect(x - 2, y - 14, ctx.measureText(d.text || 'note').width + 6, 20); }
      }

      if (d.source === 'agent' && d.agent_note && selected) {
        ctx.setLineDash([]);
        ctx.font = '10px ui-sans-serif, system-ui';
        ctx.fillStyle = '#f5c451';
        ctx.fillText(`✦ ${d.agent_note}`, 8, h - 10);
      }
      ctx.restore();
    };

    drawingsRef.current.forEach((d) => drawOne(d));
    if (draftRef.current) drawOne(draftRef.current, true);
  }, [containerRef, priceToY, timeToX, priceFmt]);

  // Keep a repaint cadence that also catches price-axis auto-scale (no event).
  useEffect(() => {
    let raf = 0;
    const loop = () => { repaint(); raf = window.setTimeout(() => requestAnimationFrame(loop), 250) as unknown as number; };
    loop();
    return () => { clearTimeout(raf); };
  }, [repaint]);

  // Rebind to chart pan/zoom + container resize whenever the chart is recreated.
  useEffect(() => {
    const chart = chartApiRef.current;
    const container = containerRef.current;
    if (!chart || !container) return;
    const onRange = () => repaint();
    chart.timeScale().subscribeVisibleTimeRangeChange(onRange);
    const ro = new ResizeObserver(() => repaint());
    ro.observe(container);
    repaint();
    return () => {
      try { chart.timeScale().unsubscribeVisibleTimeRangeChange(onRange); } catch {}
      ro.disconnect();
    };
  }, [chartApiRef, containerRef, chartInitCount, repaint]);

  // ── Persistence ─────────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    suppressSaveRef.current = true;
    setSelectedId(null);
    apiClient.kronos.drawings.get(exchange, symbol, timeframe)
      .then((r) => { if (!cancelled) setDrawings((r.data?.drawings ?? []) as Drawing[]); })
      .catch(() => { if (!cancelled) setDrawings([]); });
    return () => { cancelled = true; };
  }, [exchange, symbol, timeframe]);

  // Debounced auto-save on any change (skips the programmatic load/refine sets).
  useEffect(() => {
    if (suppressSaveRef.current) { suppressSaveRef.current = false; return; }
    setSaveState('saving');
    const id = setTimeout(() => {
      apiClient.kronos.drawings.save(exchange, symbol, { timeframe, drawings })
        .then(() => setSaveState('saved'))
        .catch(() => setSaveState('idle'));
    }, 800);
    return () => clearTimeout(id);
  }, [drawings, exchange, symbol, timeframe]);

  // ── Hit testing (select / erase) ────────────────────────────────────────────
  const distToSeg = (px: number, py: number, x1: number, y1: number, x2: number, y2: number) => {
    const dx = x2 - x1, dy = y2 - y1;
    const len2 = dx * dx + dy * dy || 1;
    let t = ((px - x1) * dx + (py - y1) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    const cx = x1 + t * dx, cy = y1 + t * dy;
    return Math.hypot(px - cx, py - cy);
  };

  const hitTest = useCallback((x: number, y: number): Drawing | null => {
    const TOL = 7;
    const list = drawingsRef.current;
    for (let i = list.length - 1; i >= 0; i--) {
      const d = list[i];
      const pts = d.points;
      if (d.type === 'hline' && pts[0]) {
        const ly = priceToY(pts[0].price);
        if (ly != null && Math.abs(y - ly) < TOL) return d;
      } else if ((d.type === 'trendline' || d.type === 'ray') && pts.length >= 2) {
        const x1 = timeToX(pts[0].time), y1 = priceToY(pts[0].price);
        let x2 = timeToX(pts[1].time), y2 = priceToY(pts[1].price);
        if (x1 == null || y1 == null || x2 == null || y2 == null) continue;
        if (d.type === 'ray' && x2 !== x1 && x2 >= x1) {
          const slope = (y2 - y1) / (x2 - x1);
          const w = containerRef.current?.clientWidth ?? x2;
          y2 = y1 + slope * (w - x1); x2 = w;
        }
        if (distToSeg(x, y, x1, y1, x2, y2) < TOL) return d;
      } else if (d.type === 'rectangle' && pts.length >= 2) {
        const x1 = timeToX(pts[0].time), y1 = priceToY(pts[0].price);
        const x2 = timeToX(pts[1].time), y2 = priceToY(pts[1].price);
        if (x1 == null || y1 == null || x2 == null || y2 == null) continue;
        const rx = Math.min(x1, x2), ry = Math.min(y1, y2), rw = Math.abs(x2 - x1), rh = Math.abs(y2 - y1);
        const near = Math.abs(x - rx) < TOL || Math.abs(x - (rx + rw)) < TOL || Math.abs(y - ry) < TOL || Math.abs(y - (ry + rh)) < TOL;
        if (near && x > rx - TOL && x < rx + rw + TOL && y > ry - TOL && y < ry + rh + TOL) return d;
      } else if (d.type === 'fib' && pts.length >= 2) {
        const pHi = Math.max(pts[0].price, pts[1].price), pLo = Math.min(pts[0].price, pts[1].price);
        for (const lv of FIB_LEVELS) {
          const ly = priceToY(pHi - (pHi - pLo) * lv);
          if (ly != null && Math.abs(y - ly) < TOL) return d;
        }
      } else if (d.type === 'text' && pts[0]) {
        const tx = timeToX(pts[0].time), ty = priceToY(pts[0].price);
        if (tx != null && ty != null && Math.abs(x - tx) < 60 && Math.abs(y - ty) < 14) return d;
      }
    }
    return null;
  }, [priceToY, timeToX, containerRef]);

  // ── Pointer interactions ─────────────────────────────────────────────────────
  const localPoint = (e: React.PointerEvent) => {
    const rect = canvasRef.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };

  const onPointerDown = (e: React.PointerEvent) => {
    const t = toolRef.current;
    if (t === 'pan') return;
    const { x, y } = localPoint(e);
    const price = yToPrice(y);
    const time = xToTime(x);
    if (price == null) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);

    if (t === 'eraser') {
      const hit = hitTest(x, y);
      if (hit) removeDrawing(hit.id);
      return;
    }
    if (t === 'select') {
      const hit = hitTest(x, y);
      setSelectedId(hit ? hit.id : null);
      if (hit && !hit.locked && time != null) dragRef.current = { id: hit.id, last: { time, price } };
      return;
    }
    if (t === 'text') {
      const txt = window.prompt('Note text:');
      if (txt && time != null) {
        addDrawing({ id: genId(), type: 'text', points: [{ time, price }], color, width: 2, text: txt, source: 'user' });
      }
      setTool('pan');
      return;
    }
    if (t === 'hline') {
      addDrawing({ id: genId(), type: 'hline', points: [{ time: time ?? 0, price }], color, width: 2, source: 'user' });
      return;
    }
    // Two-point tools: begin a draft.
    if (time == null) return;
    const type = t === 'ray' ? 'ray' : t === 'rectangle' ? 'rectangle' : t === 'fib' ? 'fib' : 'trendline';
    draftRef.current = { id: genId(), type, points: [{ time, price }, { time, price }], color, width: 2, source: 'user' };
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const { x, y } = localPoint(e);
    const price = yToPrice(y);
    const time = xToTime(x);
    if (draftRef.current && price != null && time != null) {
      draftRef.current.points[1] = { time, price };
      repaint();
      return;
    }
    if (dragRef.current && price != null && time != null) {
      const drag = dragRef.current;
      const dt = time - drag.last.time;
      const dp = price - drag.last.price;
      drag.last = { time, price };
      setDrawings((prev) => prev.map((d) => d.id === drag.id
        ? { ...d, points: d.points.map((p) => ({ time: p.time + dt, price: p.price + dp })) }
        : d));
    }
  };

  const onPointerUp = () => {
    if (draftRef.current) {
      const d = draftRef.current;
      draftRef.current = null;
      // Discard zero-length drags.
      const [a, b] = d.points;
      if (a && b && (Math.abs(a.time - b.time) > 1 || Math.abs(a.price - b.price) > 0)) {
        addDrawing(d);
        setTool('pan');
      } else {
        repaint();
      }
    }
    dragRef.current = null;
  };

  // ── Mutations ─────────────────────────────────────────────────────────────
  const addDrawing = (d: Drawing) => { setDrawings((prev) => [...prev, d]); setSelectedId(d.id); };
  const removeDrawing = (id: string) => {
    setDrawings((prev) => prev.filter((d) => d.id !== id && d.refined_from !== id));
    if (selectedRef.current === id) setSelectedId(null);
    apiClient.kronos.drawings.remove(exchange, symbol, id, timeframe).catch(() => {});
  };
  const clearAll = () => {
    if (!drawingsRef.current.length) return;
    if (!window.confirm('Remove all drawings on this chart?')) return;
    suppressSaveRef.current = true;
    setDrawings([]); setSelectedId(null);
    apiClient.kronos.drawings.clear(exchange, symbol, timeframe).catch(() => {});
  };
  const refine = () => {
    setRefining(true); setAgentNote(null);
    apiClient.kronos.drawings.refine(exchange, symbol, timeframe)
      .then((r) => {
        suppressSaveRef.current = true;
        setDrawings((r.data?.drawings ?? []) as Drawing[]);
        setAgentNote(r.data?.note ?? null);
      })
      .catch(() => setAgentNote('Agents could not refine right now.'))
      .finally(() => setRefining(false));
  };

  // Delete key removes the selected shape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === 'Delete' || e.key === 'Backspace') && selectedRef.current) {
        e.preventDefault();
        removeDrawing(selectedRef.current);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const userCount = drawings.filter((d) => d.source === 'user').length;
  const agentCount = drawings.filter((d) => d.source === 'agent').length;

  return (
    <>
      {/* Toolbar — neat horizontal bar that sits above the chart. */}
      <div className="mb-2 flex flex-wrap items-center gap-1 rounded-lg border border-gray-700 bg-gray-900/70 px-2 py-1.5">
        <div className="flex items-center gap-0.5">
          {TOOLS.map((tt) => {
            const Icon = tt.icon;
            const active = tool === tt.id;
            return (
              <button
                key={tt.id}
                type="button"
                title={tt.label}
                aria-label={tt.label}
                aria-pressed={active}
                onClick={() => { setTool(tt.id); if (tt.id !== 'select') setSelectedId(null); }}
                className={`p-1.5 rounded transition-colors ${active ? 'bg-cyan-600 text-white' : 'text-gray-300 hover:bg-gray-700 hover:text-white'}`}
              >
                <Icon className="w-4 h-4" />
              </button>
            );
          })}
        </div>

        <div className="mx-1 h-5 w-px bg-gray-700" />

        {/* Colour swatches */}
        <div className="flex items-center gap-1">
          {SWATCHES.map((c) => (
            <button
              key={c}
              type="button"
              title={`Colour ${c}`}
              aria-label={`Colour ${c}`}
              onClick={() => setColor(c)}
              className={`w-3.5 h-3.5 rounded-full border-2 transition-transform hover:scale-110 ${color === c ? 'border-white' : 'border-transparent'}`}
              style={{ backgroundColor: c }}
            />
          ))}
        </div>

        <div className="mx-1 h-5 w-px bg-gray-700" />

        <button
          type="button"
          title="Refine my drawings with the agents"
          aria-label="Refine drawings with agents"
          onClick={refine}
          disabled={refining || userCount === 0}
          className="flex items-center gap-1 rounded px-2 py-1.5 text-xs font-medium text-amber-300 hover:bg-amber-600/20 disabled:opacity-40"
        >
          {refining ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
          <span className="hidden sm:inline">Refine</span>
        </button>
        <button
          type="button"
          title="Clear all drawings"
          aria-label="Clear all drawings"
          onClick={clearAll}
          disabled={drawings.length === 0}
          className="p-1.5 rounded text-red-300 hover:bg-red-600/20 disabled:opacity-40"
        >
          <Trash2 className="w-4 h-4" />
        </button>

        {/* Save status + fullscreen pinned to the right. */}
        <div className="ml-auto flex items-center gap-2">
          <span className="text-[10px] text-gray-400 flex items-center gap-1 whitespace-nowrap">
            {saveState === 'saving' ? (<><Loader2 className="w-3 h-3 animate-spin" /> Saving…</>)
              : saveState === 'saved' ? (<><Check className="w-3 h-3 text-green-400" /> Saved</>)
              : `${userCount} drawing${userCount === 1 ? '' : 's'}`}
            {agentCount > 0 && <span className="text-amber-300">· {agentCount} ✦</span>}
          </span>
          <button
            type="button"
            title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen chart'}
            aria-label={isFullscreen ? 'Exit fullscreen' : 'Fullscreen chart'}
            onClick={onToggleFullscreen}
            className="p-1.5 rounded text-gray-300 hover:bg-gray-700 hover:text-white"
          >
            {isFullscreen ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
          </button>
        </div>
      </div>

      {/* Drawing surface + agent note — portalled onto the chart so the canvas
          stays pixel-aligned with the candles regardless of the toolbar. */}
      {portalReady && containerRef.current && createPortal(
        <>
          <canvas
            ref={canvasRef}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            className="absolute inset-0 z-10"
            style={{
              pointerEvents: tool === 'pan' ? 'none' : 'auto',
              cursor: tool === 'pan' ? 'default' : tool === 'select' ? 'move' : tool === 'eraser' ? 'not-allowed' : 'crosshair',
            }}
          />
          {agentNote && (
            <div className="absolute bottom-2 left-2 z-20 pointer-events-none">
              <span className="text-[10px] px-2 py-0.5 rounded bg-amber-900/70 text-amber-200 max-w-xs truncate">{agentNote}</span>
            </div>
          )}
        </>,
        containerRef.current,
      )}
    </>
  );
}
