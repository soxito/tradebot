/**
 * TradingRoomScene — React shell around the Three.js agent boardroom.
 *
 * Seat data is handed to the scene through a ref-backed getter so incoming SSE
 * events never rebuild the WebGL context; the animation loop just reads the
 * latest array on its next frame.
 */
import { useEffect, useRef, useState } from 'react'
import { detectStaticProfile } from '@/utils/devicePerformance'
import { prefersReducedMotion } from '@/three/variantScene'
import {
  createTradingRoom,
  type SeatInput,
  type TradingRoomHandle,
} from '@/three/tradingRoom'
import type { BoardQuote, ChartCandle, NewsItem, ScreenInfo, CycleScreenInfo } from '@/three/roomFurniture'
import type { SpeechTurn } from '@/three/tradingRoom'

interface Props {
  seats: SeatInput[]
  focusedRole: string | null
  onSeatClick?: (role: string) => void
  /** Shown on the wall screens. Repainted only when the values change. */
  screenInfo?: ScreenInfo
  /** Live quotes for the big price board on the trading-floor wall. */
  quotes?: BoardQuote[]
  /** Real OHLC bars for the back-wall chart of the focused pair. */
  chartCandles?: ChartCandle[]
  /** Recent headlines for the news screen. */
  news?: NewsItem[]
  /** The line currently being spoken at the board (drives the speech bubbles). */
  speech?: SpeechTurn | null
  /** The Bitcoin cycle read painted on the back-wall chart. */
  cycleInfo?: CycleScreenInfo | null
  /** Pause rendering when the room is off-screen (agents keep working server-side). */
  paused?: boolean
  showFps?: boolean
  className?: string
}

const EMPTY_SCREEN: ScreenInfo = { symbol: null, action: null, confidence: null, detail: null }

export default function TradingRoomScene({
  seats,
  focusedRole,
  onSeatClick,
  screenInfo,
  quotes,
  chartCandles,
  news,
  speech,
  cycleInfo,
  paused = false,
  showFps = false,
  className = '',
}: Props) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const handleRef = useRef<TradingRoomHandle | null>(null)
  const seatsRef = useRef<SeatInput[]>(seats)
  const clickRef = useRef(onSeatClick)
  const screenRef = useRef<ScreenInfo>(screenInfo ?? EMPTY_SCREEN)
  const quotesRef = useRef<BoardQuote[]>(quotes ?? [])
  const candlesRef = useRef<ChartCandle[]>(chartCandles ?? [])
  const newsRef = useRef<NewsItem[]>(news ?? [])
  const speechRef = useRef<SpeechTurn | null>(speech ?? null)
  const cycleInfoRef = useRef<CycleScreenInfo | null>(cycleInfo ?? null)
  const [fps, setFps] = useState(0)
  // Probed once up front rather than inferred from a failed build, so the
  // fallback message is a render decision instead of an effect side effect.
  const [webglOk] = useState(() => {
    if (typeof document === 'undefined') return true
    try {
      const probe = document.createElement('canvas')
      return Boolean(probe.getContext('webgl2') ?? probe.getContext('webgl'))
    } catch {
      return false
    }
  })

  useEffect(() => { seatsRef.current = seats }, [seats])
  useEffect(() => { clickRef.current = onSeatClick }, [onSeatClick])
  useEffect(() => { screenRef.current = screenInfo ?? EMPTY_SCREEN }, [screenInfo])
  useEffect(() => { quotesRef.current = quotes ?? [] }, [quotes])
  useEffect(() => { candlesRef.current = chartCandles ?? [] }, [chartCandles])
  useEffect(() => { newsRef.current = news ?? [] }, [news])
  useEffect(() => { speechRef.current = speech ?? null }, [speech])
  useEffect(() => { cycleInfoRef.current = cycleInfo ?? null }, [cycleInfo])

  useEffect(() => {
    const mount = mountRef.current
    const canvas = canvasRef.current
    if (!mount || !canvas || !webglOk) return

    const profile = detectStaticProfile()
    const handle = createTradingRoom({
      canvas,
      width: mount.clientWidth || 800,
      height: mount.clientHeight || 600,
      dpr: window.devicePixelRatio || 1,
      gfx: {
        antialias: profile.antialias,
        dprCap: profile.robotDprCap ?? profile.dprCap,
        shadows: profile.shadows,
        fpsTarget: profile.fpsTarget,
      },
      reducedMotion: prefersReducedMotion(),
      getSeats: () => seatsRef.current,
      getScreenInfo: () => screenRef.current,
      getQuotes: () => quotesRef.current,
      getChartCandles: () => candlesRef.current,
      getNews: () => newsRef.current,
      getSpeech: () => speechRef.current,
      getCycleInfo: () => cycleInfoRef.current,
      onSeatClick: (role) => clickRef.current?.(role),
    })

    if (!handle) return
    handleRef.current = handle

    const observer = new ResizeObserver(() => {
      handle.resize(mount.clientWidth, mount.clientHeight)
    })
    observer.observe(mount)

    const fpsTimer = window.setInterval(() => setFps(handle.getFps()), 1000)

    return () => {
      window.clearInterval(fpsTimer)
      observer.disconnect()
      handle.dispose()
      handleRef.current = null
    }
  }, [webglOk])

  useEffect(() => { handleRef.current?.setPaused(paused) }, [paused])
  useEffect(() => { handleRef.current?.focusSeat(focusedRole) }, [focusedRole])

  return (
    <div ref={mountRef} className={`relative h-full w-full ${className}`}>
      <canvas ref={canvasRef} className="block h-full w-full" />
      {!webglOk && (
        <div className="absolute inset-0 grid place-items-center px-6 text-center text-sm text-slate-400">
          WebGL unavailable — the agent panels still update live.
        </div>
      )}
      {showFps && webglOk && (
        <div className="pointer-events-none absolute bottom-2 right-3 font-mono text-[10px] text-cyan-300/70">
          {fps} fps
        </div>
      )}
    </div>
  )
}
