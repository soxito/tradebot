/**
 * Candles for one backtested pair with every trade drawn on them: an arrow at
 * each entry, a dot at each exit (green win, red loss, grey break-even).
 */
import { useEffect, useRef } from 'react'
import { createChart, type IChartApi, type SeriesMarker, type Time } from 'lightweight-charts'

export interface BacktestTrade {
  status: string
  action: 'buy' | 'sell'
  kind?: string
  entry_time: number
  exit_time?: number
  fill?: number
  initial_stop?: number
  tp1?: number
  tp3?: number
  exit_reason?: string
  outcome?: 'win' | 'loss' | 'breakeven'
  r: number
  pnl: number
  equity_after?: number
  reached?: number
  fills?: { time: number; price: number; fraction: number; reason: string }[]
}

export default function BacktestChart({
  candles, trades, focusTime,
}: {
  candles: [number, number, number, number, number][]
  trades: BacktestTrade[]
  focusTime?: number | null
}) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)

  useEffect(() => {
    if (!ref.current) return
    const chart = createChart(ref.current, {
      width: ref.current.clientWidth || 600,
      height: 380,
      layout: { background: { color: '#0b1020' }, textColor: '#cbd5e1' },
      grid: { vertLines: { color: '#162036' }, horzLines: { color: '#162036' } },
      rightPriceScale: { borderColor: '#243049' },
      timeScale: { borderColor: '#243049', timeVisible: true },
    })
    chartRef.current = chart
    const series = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444', wickUpColor: '#22c55e', wickDownColor: '#ef4444', borderVisible: false,
    })
    series.setData(candles.map(([t, o, h, l, c]) => ({ time: t as Time, open: o, high: h, low: l, close: c })))
    const markers: SeriesMarker<Time>[] = []
    for (const t of trades) {
      if (t.status !== 'closed') continue
      markers.push({
        time: t.entry_time as Time,
        position: t.action === 'buy' ? 'belowBar' : 'aboveBar',
        color: t.action === 'buy' ? '#38bdf8' : '#f472b6',
        shape: t.action === 'buy' ? 'arrowUp' : 'arrowDown',
        text: t.action.toUpperCase(),
      })
      if (t.exit_time) {
        markers.push({
          time: t.exit_time as Time,
          position: t.action === 'buy' ? 'aboveBar' : 'belowBar',
          color: t.outcome === 'win' ? '#22c55e' : t.outcome === 'loss' ? '#ef4444' : '#94a3b8',
          shape: 'circle',
          text: `${t.r >= 0 ? '+' : ''}${t.r.toFixed(2)}R`,
        })
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number))
    series.setMarkers(markers)
    chart.timeScale().fitContent()
    const onResize = () => ref.current && chart.applyOptions({ width: ref.current.clientWidth })
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
      chartRef.current = null
    }
  }, [candles, trades])

  useEffect(() => {
    if (!focusTime || !chartRef.current || !candles.length) return
    const idx = candles.findIndex((c) => c[0] >= focusTime)
    if (idx < 0) return
    chartRef.current.timeScale().setVisibleLogicalRange({ from: Math.max(0, idx - 60), to: idx + 60 })
  }, [focusTime, candles])

  return <div ref={ref} className="w-full overflow-hidden rounded-lg border border-slate-800" />
}
