import { useEffect, useRef, useState, useCallback } from 'react';
import { createChart, IChartApi, ISeriesApi, SeriesMarker, Time } from 'lightweight-charts';
import { apiClient } from '@/services/api';
import { calcMinMove, calcChartPrecision } from '@/utils/price';
import { formatTimeZA } from '@/utils/datetime';

interface PriceLine {
  price: number;
  color: string;
  title: string;
  lineWidth?: number;
  lineStyle?: number;
  axisLabelVisible?: boolean;
}

interface SimPositionOverlay {
  symbol: string;
  side: string;
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
  amount: number;
  unrealized_pnl: number;
}

export interface IndicatorOverlaySeries {
  name: string;
  type: 'line' | 'histogram';
  pane: string;       // 'main' = price chart, else separate pane name
  color: string;
  lineWidth?: number;
  lineStyle?: number;
  data: { time: number; value: number }[];
  // For MACD histogram type
  signal_data?: { time: number; value: number }[];
  histogram_data?: { time: number; value: number; color?: string }[];
  signal_color?: string;
  // For sub-pane indicators
  levels?: { value: number; color: string; label: string }[];
  extra_lines?: { name: string; color: string; data: { time: number; value: number }[] }[];
}

export interface IndicatorMarker {
  time: number;
  position: 'belowBar' | 'aboveBar' | 'inBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square';
  text: string;
}

export interface LimitOrderOverlay {
  orderId: string;
  symbol: string;
  side: string;
  price: number;
  size: string;
  orderType: string;
  stopLoss?: number;
  takeProfit?: number;
}

interface TradingViewChartProps {
  symbol: string;
  exchange?: string;
  timeframe?: string;
  simPositions?: SimPositionOverlay[];
  limitOrders?: LimitOrderOverlay[];
  overlays?: IndicatorOverlaySeries[];
  markers?: IndicatorMarker[];
  strategyName?: string;
  strategyScore?: number;
  strategyAction?: string;
  maximized?: boolean;
  onToggleMaximize?: () => void;
  onSlTpChange?: (symbol: string, side: string, sl: number | null, tp: number | null) => void;
}

export default function TradingViewChart({ 
  symbol, 
  exchange = 'bitget',
  timeframe = '1h',
  simPositions = [],
  limitOrders = [],
  overlays = [],
  markers = [],
  strategyName,
  strategyScore,
  strategyAction,
  maximized = false,
  onToggleMaximize,
  onSlTpChange,
}: TradingViewChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlestickSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const overlaySeriesRef = useRef<ISeriesApi<any>[]>([]);
  const priceLinesRef = useRef<any[]>([]);
  const orderLinesRef = useRef<any[]>([]);
  // True until the candles have been fitted once for the current chart instance.
  // Prevents the 60s background refresh from resetting the user's zoom/pan.
  const didFitRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  // Drag state for SL/TP editing
  const [editingSl, setEditingSl] = useState<number | null>(null);
  const [editingTp, setEditingTp] = useState<number | null>(null);
  const [editingPosition, setEditingPosition] = useState<SimPositionOverlay | null>(null);
  const [saving, setSaving] = useState(false);
  const dragTypeRef = useRef<'sl' | 'tp' | null>(null);
  const slLineRef = useRef<any>(null);
  const tpLineRef = useRef<any>(null);

  // Fetch real market data
  const fetchChartData = async () => {
    try {
      setError(null);
      const response = await apiClient.getOHLCV(
        exchange,
        symbol,
        timeframe,
        200  // Fetch 200 candles for good chart history
      );
      
      if (response.data?.data && candlestickSeriesRef.current) {
        const data = response.data.data;
        candlestickSeriesRef.current.setData(data);

        // Auto-detect price precision from OHLCV data (handles PEPE, SHIB etc.)
        const allPrices = data.flatMap((c: any) => [c.open, c.high, c.low, c.close]).filter((p: number) => p > 0);
        if (allPrices.length > 0) {
          const precision = calcChartPrecision(allPrices);
          const minMove = calcMinMove(allPrices);
          candlestickSeriesRef.current.applyOptions({
            priceFormat: {
              type: 'price',
              precision,
              minMove,
            },
          });
        }

        // Fit candles to the visible area once on first load so the chart draws
        // nicely instead of showing a cramped/partial view. Subsequent background
        // refreshes preserve the user's zoom/pan.
        if (!didFitRef.current) {
          chartRef.current?.timeScale().fitContent();
          didFitRef.current = true;
        }

        setLastUpdate(new Date());
        setLoading(false);
      }
    } catch (err: any) {
      console.error('Failed to fetch chart data:', err);
      setError(err.response?.data?.detail || 'Failed to load chart data');
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!chartContainerRef.current) return;

    // Create chart. Fall back to a sensible width when the container has not
    // been laid out yet (clientWidth can be 0 inside a hidden tab/grid cell),
    // otherwise the chart paints blank until the window is resized.
    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth || 600,
      height: maximized ? window.innerHeight - 120 : 400,
      layout: {
        background: { color: '#0a0e27' },
        textColor: '#d1d4dc',
      },
      grid: {
        vertLines: { color: '#1a1e3a' },
        horzLines: { color: '#1a1e3a' },
      },
      crosshair: {
        mode: 1,
      },
      rightPriceScale: {
        borderColor: '#2a2e4a',
      },
      timeScale: {
        borderColor: '#2a2e4a',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candlestickSeries = chart.addCandlestickSeries({
      upColor: '#00ff88',
      downColor: '#ff4444',
      borderDownColor: '#ff4444',
      borderUpColor: '#00ff88',
      wickDownColor: '#ff4444',
      wickUpColor: '#00ff88',
    });

    chartRef.current = chart;
    candlestickSeriesRef.current = candlestickSeries;
    didFitRef.current = false;

    // Initial data fetch
    fetchChartData();

    // Auto-refresh every 60 seconds
    const refreshInterval = setInterval(fetchChartData, 60000);

    // Keep the chart sized to its container. ResizeObserver catches layout
    // changes that a window 'resize' event misses (tab switch, sidebar toggle,
    // responsive grid reflow, or the container gaining width after mount).
    const handleResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        const width = chartContainerRef.current.clientWidth;
        if (width > 0) {
          chartRef.current.applyOptions({
            width,
            height: maximized ? window.innerHeight - 120 : 400,
          });
        }
      }
    };

    window.addEventListener('resize', handleResize);

    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(chartContainerRef.current);

    return () => {
      window.removeEventListener('resize', handleResize);
      resizeObserver.disconnect();
      clearInterval(refreshInterval);
      chart.remove();
    };
  }, [symbol, exchange, timeframe, maximized]);

  // Draw indicator overlays on the chart.
  // Custom mode does not support true multi-pane rendering, so non-main panes
  // are displayed on the main pane as a graceful fallback.
  useEffect(() => {
    const chart = chartRef.current;
    const series = candlestickSeriesRef.current;
    if (!chart || !series) return;

    // Remove old overlay series
    overlaySeriesRef.current.forEach(s => {
      try { chart.removeSeries(s); } catch {}
    });
    overlaySeriesRef.current = [];

    const normalizeLineData = (data: { time: number; value: number }[] | undefined) => {
      return (data || [])
        .filter(point => Number.isFinite(point?.time) && Number.isFinite(point?.value))
        .map(point => ({
          time: point.time as Time,
          value: point.value,
        }));
    };

    const normalizeHistogramData = (
      data:
        | { time: number; value: number; color?: string }[]
        | { time: number; value: number }[]
        | undefined,
      fallbackColor: string
    ) => {
      return (data || [])
        .filter(point => Number.isFinite(point?.time) && Number.isFinite(point?.value))
        .map(point => ({
          time: point.time as Time,
          value: point.value,
          color: (point as { color?: string }).color || fallbackColor,
        }));
    };

    overlays.forEach(overlay => {
      const overlayType = overlay.type === 'histogram' ? 'histogram' : 'line';

      if (overlayType === 'histogram') {
        const histogramData = normalizeHistogramData(
          overlay.histogram_data && overlay.histogram_data.length > 0
            ? overlay.histogram_data
            : overlay.data,
          overlay.color
        );
        if (histogramData.length > 0) {
          const histogramSeries = chart.addHistogramSeries({
            color: overlay.color,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          histogramSeries.setData(histogramData as any);
          overlaySeriesRef.current.push(histogramSeries);
        }

        const signalLine = normalizeLineData(overlay.signal_data);
        if (signalLine.length > 0) {
          const signalSeries = chart.addLineSeries({
            color: overlay.signal_color || '#f59e0b',
            lineWidth: 1,
            lineStyle: 0,
            crosshairMarkerVisible: false,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          signalSeries.setData(signalLine as any);
          overlaySeriesRef.current.push(signalSeries);
        }

        return;
      }

      const lineData = normalizeLineData(overlay.data);
      if (lineData.length === 0) return;

      const lineSeries = chart.addLineSeries({
        color: overlay.color,
        lineWidth: (overlay.lineWidth || 1) as 1 | 2 | 3 | 4,
        lineStyle: overlay.lineStyle || 0,
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      lineSeries.setData(lineData as any);
      overlaySeriesRef.current.push(lineSeries);

      (overlay.extra_lines || []).forEach(extra => {
        const extraData = normalizeLineData(extra.data);
        if (extraData.length === 0) return;
        const extraSeries = chart.addLineSeries({
          color: extra.color,
          lineWidth: 1,
          lineStyle: 2,
          crosshairMarkerVisible: false,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        extraSeries.setData(extraData as any);
        overlaySeriesRef.current.push(extraSeries);
      });
    });

    // Set markers on candlestick series
    if (markers.length > 0) {
      const sortedMarkers = [...markers].sort((a, b) => a.time - b.time);
      const chartMarkers: SeriesMarker<Time>[] = sortedMarkers.map(m => ({
        time: m.time as Time,
        position: m.position,
        color: m.color,
        shape: m.shape,
        text: m.text,
      }));
      series.setMarkers(chartMarkers);
    } else {
      series.setMarkers([]);
    }
  }, [overlays, markers]);

  // Draw SL/TP/Entry price lines for positions matching this symbol
  // SL/TP lines are click-to-drag editable when onSlTpChange is provided
  useEffect(() => {
    const series = candlestickSeriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;

    // Remove old price lines
    priceLinesRef.current.forEach(line => {
      try { series.removePriceLine(line); } catch {}
    });
    priceLinesRef.current = [];
    slLineRef.current = null;
    tpLineRef.current = null;

    // Draw lines for each position matching this symbol
    const matching = simPositions.filter(p => p.symbol === symbol);
    matching.forEach(pos => {
      // Entry price line
      const entryLine = series.createPriceLine({
        price: pos.entry_price,
        color: pos.side === 'long' ? '#3b82f6' : '#f59e0b',
        lineWidth: 2,
        lineStyle: 0, // Solid
        axisLabelVisible: true,
        title: `Entry ${pos.side === 'long' ? '▲' : '▼'} ${pos.amount.toFixed(4)}`,
      });
      priceLinesRef.current.push(entryLine);

      const slPrice = editingSl !== null ? editingSl : pos.stop_loss;
      const tpPrice = editingTp !== null ? editingTp : pos.take_profit;
      const isEditable = !!onSlTpChange;

      // Stop-loss line
      if (slPrice) {
        const slLine = series.createPriceLine({
          price: slPrice,
          color: editingSl !== null ? '#fbbf24' : '#ef4444',
          lineWidth: 2,
          lineStyle: editingSl !== null ? 0 : 2,
          axisLabelVisible: true,
          title: isEditable
            ? `SL ✕ ${editingSl !== null ? '(drag)' : '⇅ click to edit'}`
            : `SL ✕`,
        });
        priceLinesRef.current.push(slLine);
        slLineRef.current = slLine;
      }

      // Take-profit line
      if (tpPrice) {
        const tpLine = series.createPriceLine({
          price: tpPrice,
          color: editingTp !== null ? '#fbbf24' : '#22c55e',
          lineWidth: 2,
          lineStyle: editingTp !== null ? 0 : 2,
          axisLabelVisible: true,
          title: isEditable
            ? `TP ✓ ${editingTp !== null ? '(drag)' : '⇅ click to edit'}`
            : `TP ✓`,
        });
        priceLinesRef.current.push(tpLine);
        tpLineRef.current = tpLine;
      }

      // Remember which position we're editing
      if (isEditable && !editingPosition) {
        setEditingPosition(pos);
      }
    });
  }, [simPositions, symbol, editingSl, editingTp]);

  // Draw limit order price lines
  // Colors are deliberately distinct from position lines:
  //   Position entry:   solid blue/amber, lineWidth 2
  //   Position SL/TP:   dashed red/green, lineWidth 2
  //   Limit entry:      dashed cyan/magenta, lineWidth 1  (thinner + unique hue)
  //   Limit SL/TP:      dotted dim yellow/teal, lineWidth 1, axis label OFF
  useEffect(() => {
    const series = candlestickSeriesRef.current;
    if (!series) return;

    // Remove old order lines
    orderLinesRef.current.forEach(line => {
      try { series.removePriceLine(line); } catch {}
    });
    orderLinesRef.current = [];

    // Draw lines for each limit order matching this symbol
    const matching = limitOrders.filter(o => o.symbol === symbol);
    matching.forEach(order => {
      const isBuy = order.side?.toLowerCase().includes('buy');
      const coinLabel = order.symbol.split('/')[0] || '';
      // Limit entry: cyan for buy, magenta for sell (avoids amber clash with position entry)
      const entryLine = series.createPriceLine({
        price: order.price,
        color: isBuy ? '#06b6d4' : '#d946ef',
        lineWidth: 1,
        lineStyle: 2, // Dashed
        axisLabelVisible: true,
        title: `⏳ ${coinLabel} ${isBuy ? 'BUY' : 'SELL'} ×${order.size}`,
      });
      orderLinesRef.current.push(entryLine);

      // SL for this limit order — dim yellow, dotted, no axis label (avoid clutter)
      if (order.stopLoss && order.stopLoss > 0) {
        const slLine = series.createPriceLine({
          price: order.stopLoss,
          color: '#fbbf2466',
          lineWidth: 1,
          lineStyle: 3, // Dotted
          axisLabelVisible: false,
          title: `⏳ ${coinLabel} SL`,
        });
        orderLinesRef.current.push(slLine);
      }

      // TP for this limit order — dim teal, dotted, no axis label
      if (order.takeProfit && order.takeProfit > 0) {
        const tpLine = series.createPriceLine({
          price: order.takeProfit,
          color: '#2dd4bf66',
          lineWidth: 1,
          lineStyle: 3, // Dotted
          axisLabelVisible: false,
          title: `⏳ ${coinLabel} TP`,
        });
        orderLinesRef.current.push(tpLine);
      }
    });
  }, [limitOrders, symbol]);

  // Allow clicking near SL/TP line to start editing, then crosshair move updates price
  useEffect(() => {
    const chart = chartRef.current;
    const series = candlestickSeriesRef.current;
    if (!chart || !series || !onSlTpChange) return;

    const handleClick = (param: any) => {
      if (!param.point || saving) return;
      const price = series.coordinateToPrice(param.point.y);
      if (price == null || price <= 0) return;

      const matching = simPositions.filter(p => p.symbol === symbol);
      if (matching.length === 0) return;
      const pos = matching[0];

      const currentSl = editingSl !== null ? editingSl : pos.stop_loss;
      const currentTp = editingTp !== null ? editingTp : pos.take_profit;

      // If already dragging, clicking sets the new value
      if (dragTypeRef.current === 'sl') {
        dragTypeRef.current = null;
        setEditingSl(price);
        return;
      }
      if (dragTypeRef.current === 'tp') {
        dragTypeRef.current = null;
        setEditingTp(price);
        return;
      }

      // Check proximity to SL or TP line
      const threshold = Math.abs((pos.entry_price || price) * 0.003); // 0.3% of entry

      if (currentSl && Math.abs(price - currentSl) < threshold) {
        dragTypeRef.current = 'sl';
        setEditingSl(currentSl);
        setEditingPosition(pos);
        return;
      }
      if (currentTp && Math.abs(price - currentTp) < threshold) {
        dragTypeRef.current = 'tp';
        setEditingTp(currentTp);
        setEditingPosition(pos);
        return;
      }
    };

    const handleCrosshairMove = (param: any) => {
      if (!param.point || !dragTypeRef.current) return;
      const price = series.coordinateToPrice(param.point.y);
      if (price == null || price <= 0) return;

      if (dragTypeRef.current === 'sl') {
        setEditingSl(price);
      } else if (dragTypeRef.current === 'tp') {
        setEditingTp(price);
      }
    };

    chart.subscribeClick(handleClick);
    chart.subscribeCrosshairMove(handleCrosshairMove);

    return () => {
      chart.unsubscribeClick(handleClick);
      chart.unsubscribeCrosshairMove(handleCrosshairMove);
    };
  }, [simPositions, symbol, onSlTpChange, editingSl, editingTp, saving]);

  // Save edited SL/TP
  const handleSaveSlTp = useCallback(async () => {
    if (!editingPosition || !onSlTpChange) return;
    setSaving(true);
    dragTypeRef.current = null;
    try {
      await onSlTpChange(
        editingPosition.symbol,
        editingPosition.side,
        editingSl,
        editingTp,
      );
    } finally {
      setSaving(false);
      setEditingSl(null);
      setEditingTp(null);
      setEditingPosition(null);
    }
  }, [editingPosition, editingSl, editingTp, onSlTpChange]);

  // Cancel editing
  const handleCancelEdit = useCallback(() => {
    dragTypeRef.current = null;
    setEditingSl(null);
    setEditingTp(null);
    setEditingPosition(null);
  }, []);

  return (
    <div className="w-full">
      <div className="mb-2 flex justify-between items-center">
        <div>
          <h3 className="text-lg font-semibold">{symbol}</h3>
          <div className="text-xs text-gray-500">
            {exchange.toUpperCase()} • {timeframe}
            {strategyName && (
              <span className="ml-2 text-cyan-400">• {strategyName}</span>
            )}
          </div>
        </div>
        <div className="text-right flex items-center gap-3">
          {strategyScore !== undefined && (
            <div className="flex items-center gap-2">
              <span className={`text-sm font-bold px-2 py-0.5 rounded ${
                strategyAction === 'buy' ? 'bg-green-500/20 text-green-400' :
                strategyAction === 'sell' ? 'bg-red-500/20 text-red-400' :
                'bg-gray-500/20 text-gray-400'
              }`}>
                {strategyAction?.toUpperCase() || 'HOLD'}
              </span>
              <span className={`text-sm font-mono ${
                strategyScore > 0 ? 'text-green-400' : strategyScore < 0 ? 'text-red-400' : 'text-gray-400'
              }`}>
                {strategyScore > 0 ? '+' : ''}{strategyScore.toFixed(4)}
              </span>
            </div>
          )}
          {loading && (
            <div className="text-sm text-yellow-400">Loading...</div>
          )}
          {error && (
            <div className="text-sm text-red-400">
              {error}
            </div>
          )}
          {!loading && !error && lastUpdate && (
            <div className="text-xs text-gray-500">
              Updated: {formatTimeZA(lastUpdate)}
            </div>
          )}
          {onToggleMaximize && (
            <button
              onClick={onToggleMaximize}
              className="p-1.5 rounded hover:bg-gray-700 text-gray-400 hover:text-white transition"
              title={maximized ? 'Exit fullscreen' : 'Maximize chart'}
            >
              {maximized ? (
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
              ) : (
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
              )}
            </button>
          )}
        </div>
      </div>
      <div ref={chartContainerRef} className="rounded-lg overflow-hidden border border-gray-700" />

      {/* SL/TP Edit Bar — shows when actively editing */}
      {onSlTpChange && (editingSl !== null || editingTp !== null) && editingPosition && (
        <div className="mt-2 flex items-center gap-3 bg-gray-800/80 border border-yellow-500/40 rounded-lg px-4 py-2">
          <span className="text-yellow-400 text-xs font-semibold">Editing SL/TP</span>
          {editingSl !== null && (
            <span className="text-xs text-red-400 font-mono">
              SL: {editingSl.toFixed(editingSl < 1 ? 6 : editingSl < 100 ? 4 : 2)}
            </span>
          )}
          {editingTp !== null && (
            <span className="text-xs text-green-400 font-mono">
              TP: {editingTp.toFixed(editingTp < 1 ? 6 : editingTp < 100 ? 4 : 2)}
            </span>
          )}
          {dragTypeRef.current && (
            <span className="text-xs text-yellow-300 animate-pulse">Click on chart to set price</span>
          )}
          <div className="ml-auto flex gap-2">
            <button
              onClick={handleSaveSlTp}
              disabled={saving}
              className="text-xs px-3 py-1 rounded bg-green-600 hover:bg-green-500 text-white font-medium disabled:opacity-50"
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
            <button
              onClick={handleCancelEdit}
              disabled={saving}
              className="text-xs px-3 py-1 rounded bg-gray-600 hover:bg-gray-500 text-white font-medium disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Indicator Legend */}
      {overlays.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-2 text-xs">
          {overlays.map((o, i) => (
            <span key={i} className="flex items-center gap-1">
              <span className="w-3 h-0.5 inline-block rounded" style={{ backgroundColor: o.color }} />
              <span className="text-gray-400">{o.name}</span>
              {o.pane && o.pane !== 'main' && (
                <span className="text-gray-500">({o.pane})</span>
              )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
