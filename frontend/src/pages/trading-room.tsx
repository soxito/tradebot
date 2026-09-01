/**
 * Trading Room — the agent boardroom.
 *
 * A 3D table where every agent has a seat, a face and a live state, wired to the
 * orchestrator's SSE pipeline. JARVIS chairs the meeting and announces the
 * outcome; the agents keep working server-side whether or not this page is open.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import Head from 'next/head'
import Link from 'next/link'
import dynamic from 'next/dynamic'
import { Activity, Bot, ChevronDown, ChevronUp, Radio, Search, Settings, Volume2, VolumeX } from 'lucide-react'

import { useTradingRoom, type RoomSeat } from '@/hooks/useTradingRoom'
import { useBtcCycleState } from '@/hooks/useBtcCycle'
import { useStreamState } from '@/hooks/useEventStream'
import { usePageVisibility } from '@/hooks/usePageVisibility'
import { useLivePrices } from '@/hooks/useLivePrices'
import { useResearchFeed } from '@/hooks/useResearchFeed'
import { POPULAR_PAIRS } from '@/constants/tradingPairs'
import { useRoomAnnouncements } from '@/hooks/useRoomAnnouncements'
import AgentPanel from '@/components/room/AgentPanel'
import SessionStream from '@/components/room/SessionStream'
import LiveDebate from '@/components/room/LiveDebate'
import AccountTabs from '@/components/room/AccountTabs'
import PairFocusSelector from '@/components/room/PairFocusSelector'
import DeskBrief from '@/components/room/DeskBrief'
import TradingAgentsDesk from '@/components/tradingagents/TradingAgentsDesk'
import type { SeatInput } from '@/three/tradingRoom'
import type { ChartCandle, ChartOverlays, CycleBand, ChartPatternMarker, NewsItem } from '@/three/roomFurniture'

// The candles endpoint speaks MT5 timeframe labels; the room settings speak
// ccxt ones. One mapping, so both surfaces name the same bar.
const TF_TO_MT5: Record<string, string> = {
  '5m': 'M5', '15m': 'M15', '30m': 'M30', '1h': 'H1', '4h': 'H4', '1d': 'D1',
}
import { api, apiClient } from '@/services/api'

const TradingRoomScene = dynamic(() => import('@/components/TradingRoomScene'), { ssr: false })

function toSceneSeats(seats: RoomSeat[]): SeatInput[] {
  return seats.map((s) => ({
    role: s.role,
    human_name: s.human_name,
    title: s.title,
    color: s.color,
    seat: s.seat,
    state: s.state,
    confidence: s.last_decision?.confidence ?? 0,
    action: s.last_decision?.action,
    gender: s.gender,
  }))
}

export default function TradingRoomPage() {
  const {
    seats, sessions, focusSymbol, focusSymbols, ceo, loaded, lastCompleted, executions,
    debate, workerRunning, setFocus, toggleFocus, clearFocus, toggleWorker,
  } = useTradingRoom()
  const streamState = useStreamState()
  const visible = usePageVisibility()
  const [focusedRole, setFocusedRole] = useState<string | null>(null)
  const [voiceOn, setVoiceOn] = useState(true)
  // The multi-agent desk (TradingAgents framework) slides over the room when
  // summoned; it keeps streaming server-side even while collapsed.
  const [taDeskOpen, setTaDeskOpen] = useState(false)
  // Right-rail collapsible state — each card remembers its open/closed state.
  const [pairsOpen, setPairsOpen] = useState(true)
  const [orderDeskOpen, setOrderDeskOpen] = useState(true)
  const [logOpen, setLogOpen] = useState(true)

  // Research feed — calendar events appear on the news screen when no verdict is pending.
  const { data: resData } = useResearchFeed({ days: 7, fomo_only: true })

  useRoomAnnouncements(lastCompleted, { enabled: voiceOn, seats })

  const sceneSeats = useMemo(() => toSceneSeats(seats), [seats])
  const activeCount = seats.filter((s) => s.state === 'analyzing').length
  const runningSession = sessions.find((s) => s.status === 'running')

  // The line currently being spoken at the board — the newest debate turn.
  // The 3D scene applies its own freshness window each frame, so no timer is
  // needed here; the bubble fades when the turn ages out.
  const currentSpeech = useMemo(() => {
    const last = debate[debate.length - 1]
    if (!last) return null
    return { role: last.role, human_name: last.human_name, color: last.color, text: last.text, at: last.at }
  }, [debate])

  // The season the board is trading in, painted on the back-wall chart.
  const { state: cycleState } = useBtcCycleState()
  const cycleInfo = useMemo(() => {
    if (!cycleState?.ok) return null
    const days = cycleState.phase === 'bull' ? cycleState.days_to_top : cycleState.days_to_bottom
    return {
      phase: cycleState.phase,
      dayOfCycle: cycleState.day_of_cycle,
      daysToTurn: days,
    }
  }, [cycleState])

  // Live quotes for the big board. Falls back to the pairs the room can pick
  // from, so the board is never blank while the table is between sessions.
  const boardSymbols = useMemo(
    () => (focusSymbols.length ? focusSymbols : POPULAR_PAIRS).slice(0, 6),
    [focusSymbols],
  )
  const livePrices = useLivePrices(boardSymbols, visible)
  const boardQuotes = useMemo(
    () => boardSymbols.map((symbol) => ({
      symbol,
      price: livePrices[symbol]?.price ?? null,
      prev: livePrices[symbol]?.prev ?? null,
    })),
    [boardSymbols, livePrices],
  )

  // What the wall screens display: whatever the table is on right now, falling
  // back to the last thing it finished.
  const screenInfo = useMemo(() => {
    const session = runningSession ?? sessions[0]
    if (!session) {
      return { symbol: focusSymbol ?? null, action: null, confidence: null, detail: null }
    }
    const tally = session.consensus?.tally
    const votes = tally ? Object.values(tally).reduce((a, b) => a + b, 0) : 0
    const leader = session.consensus?.leader
    const leaderVotes = tally && leader && leader in tally
      ? tally[leader as keyof typeof tally]
      : 0
    return {
      symbol: session.symbol ?? focusSymbol ?? null,
      action: session.final_action ?? leader ?? null,
      confidence: session.final_confidence ?? session.consensus?.weighted_confidence ?? null,
      detail: session.status === 'running'
        ? 'in session'
        : votes && leader
          ? `${leaderVotes} of ${votes} agree`
          : null,
    }
  }, [runningSession, sessions, focusSymbol])

  // Real OHLC bars for the back-wall chart, from the same market feed the app's
  // other charts use — so the boardroom screen is accurate, not synthesised.
  //
  // The timeframe is the one the room settings chose, not a hard-coded H1: the
  // agents read that timeframe's candles, so drawing a different one behind
  // them put the board's argument under a chart of a different market.
  const chartSymbol = screenInfo.symbol
  const [roomTimeframe, setRoomTimeframe] = useState('1h')
  useEffect(() => {
    if (!visible) return
    let alive = true
    void (async () => {
      try {
        const res = await api.get('/agents/room/settings')
        if (alive && res.data?.focus_timeframe) setRoomTimeframe(String(res.data.focus_timeframe))
      } catch {
        /* the 1h default is a fine chart while the settings are unreachable */
      }
    })()
    return () => { alive = false }
  }, [visible])

  const [chartCandles, setChartCandles] = useState<ChartCandle[]>([])
  useEffect(() => {
    if (!chartSymbol || !visible) return
    let alive = true
    const controller = new AbortController()
    const load = async () => {
      try {
        const res = await apiClient.getMarketCandles(chartSymbol, TF_TO_MT5[roomTimeframe] ?? 'H1', 60, controller.signal)
        const raw = res.data?.candles ?? []
        if (!alive) return
        setChartCandles(raw.map((c: { time?: number; open: number; high: number; low: number; close: number }) =>
          ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })))
      } catch {
        /* leave the last bars up; the live price still tracks the header */
      }
    }
    void load()
    const timer = window.setInterval(load, 30_000)
    return () => { alive = false; controller.abort(); window.clearInterval(timer) }
  }, [chartSymbol, roomTimeframe, visible])

  // Wall-chart overlays: named candlestick patterns at the room's own
  // timeframe, plus the cycle's green/red season bands behind the candles.
  // The chart always shows the room timeframe; the bands only say what season
  // the bars were printed in.
  const [chartOverlays, setChartOverlays] = useState<ChartOverlays | null>(null)
  useEffect(() => {
    if (!chartSymbol || !visible) return
    let alive = true
    const controller = new AbortController()
    const load = async () => {
      try {
        const [pat, win] = await Promise.all([
          apiClient.getPatternOverlay('yahoo', chartSymbol.replace('/', ''), {
            timeframe: TF_TO_MT5[roomTimeframe] ?? 'H1',
            limit: 120,
            fisher: false,
          }),
          apiClient.getCycleWindows(),
        ])
        if (!alive) return
        const markers: ChartPatternMarker[] = (pat.data?.markers ?? [])
          .filter((m: { text?: string }) => Boolean(m.text))
          .map((m: { time: number; text?: string; color?: string }) => ({
            time: m.time > 10 ** 12 ? m.time : m.time * 1000,
            name: String(m.text ?? ''),
            direction: (m.color ?? '').includes('22c55e') ? 'bull' : 'bear',
          }))
        const bands: CycleBand[] = (win.data?.windows ?? []).map((w: { start: string; end: string; phase: string; projected?: boolean }) => ({
          start: new Date(`${w.start}T00:00:00Z`).getTime(),
          end: new Date(`${w.end}T00:00:00Z`).getTime(),
          phase: w.phase === 'bull' ? 'bull' : 'bear',
          projected: w.projected,
        }))
        setChartOverlays({ markers, bands })
      } catch {
        /* the wall chart stands without flags */
      }
    }
    void load()
    const timer = window.setInterval(load, 300_000)
    return () => { alive = false; controller.abort(); window.clearInterval(timer) }
  }, [chartSymbol, roomTimeframe, visible])

  // The right-wall news screen: a live wire built from the desk's own activity —
  // orders it just placed and verdicts it just reached. Newest first.
  const newsItems = useMemo<NewsItem[]>(() => {
    const toneFor = (action?: string | null): NewsItem['tone'] => {
      const a = (action ?? '').toUpperCase()
      if (a.includes('BUY') || a === 'LONG') return 'up'
      if (a.includes('SELL') || a === 'SHORT') return 'down'
      return 'neutral'
    }
    const orders: NewsItem[] = executions.slice(0, 6).map((e) => ({
      title: `${e.symbol} ${e.action?.toUpperCase() ?? ''} — ${e.reason ?? 'order routed'}`.trim(),
      tone: toneFor(e.action),
      tag: e.status === 'dry_run' ? 'DRY RUN' : e.status.toUpperCase(),
    }))
    const verdicts: NewsItem[] = sessions
      .filter((s) => s.status !== 'running' && (s.final_action || s.consensus?.leader))
      .slice(0, 6)
      .map((s) => {
        const action = s.final_action ?? s.consensus?.leader ?? null
        const conf = s.final_confidence ?? s.consensus?.weighted_confidence ?? null
        const pct = conf != null ? ` ${Math.round(conf * (conf <= 1 ? 100 : 1))}%` : ''
        return {
          title: `${s.symbol ?? '—'} verdict: ${(action ?? 'HOLD').toUpperCase()}${pct}`,
          tone: toneFor(action),
          tag: 'VERDICT',
        }
      })
    return [...orders, ...verdicts].slice(0, 6)
  }, [executions, sessions])

  // Calendar events from the research feed — shown on the news screen when no active session.
  const calItems = useMemo<NewsItem[]>(() => {
    const events = resData?.events ?? []
    return events
      .filter((e) => e.hours_away > -1 && e.hours_away < 48)
      .slice(0, 3)
      .map((e) => ({
        title: `${e.currency} — ${e.title}${e.hours_away > 0 ? `  •  in ${Math.round(e.hours_away)}h` : '  •  just now'}`,
        tone: 'neutral' as const,
        tag: (e.impact ?? 'low').toUpperCase(),
      }))
  }, [resData?.events])

  // Calendar events supplement the live news wire; session verdicts take priority.
  const allNewsItems = useMemo<NewsItem[]>(
    () => [...newsItems, ...calItems].slice(0, 6),
    [newsItems, calItems],
  )

  const selectSeat = useCallback((role: string) => {
    setFocusedRole((prev) => (prev === role ? null : role))
  }, [])

  return (
    <>
      <Head>
        <title>Trading Room — TradeBot</title>
      </Head>

      <div className="flex h-[calc(100vh-4rem)] flex-col gap-3 p-3 xl:flex-row">
        {/* ── Left: the seats ── */}
        <aside className="flex w-full shrink-0 flex-col gap-2 overflow-y-auto xl:w-72">
          <div className="rounded-xl border border-cyan-500/40 bg-gradient-to-br from-cyan-500/10 to-transparent p-3">
            <div className="flex items-center gap-2">
              <span className="grid h-9 w-9 place-items-center rounded-full bg-cyan-400 text-xs font-bold text-slate-950">
                JV
              </span>
              <div>
                <div className="text-sm font-semibold text-cyan-200">{ceo?.human_name ?? 'JARVIS'}</div>
                <div className="text-[11px] text-slate-400">{ceo?.title ?? 'Chief Executive'}</div>
              </div>
              <button
                type="button"
                onClick={() => setVoiceOn((v) => !v)}
                className="ml-auto rounded-lg border border-slate-700 p-1.5 text-slate-300 hover:border-slate-500"
                title={voiceOn ? 'Mute announcements' : 'Unmute announcements'}
              >
                {voiceOn ? <Volume2 className="h-4 w-4" /> : <VolumeX className="h-4 w-4" />}
              </button>
              <Link
                href="/trading-room-settings"
                className="rounded-lg border border-slate-700 p-1.5 text-slate-300 hover:border-slate-500"
                title="Room settings — agents, execution, risk"
              >
                <Settings className="h-4 w-4" />
              </Link>
            </div>
            <p className="mt-2 text-[11px] leading-snug text-slate-400">
              {runningSession
                ? `Chairing analysis on ${runningSession.symbol} — ${activeCount} agent(s) speaking.`
                : 'Table is quiet. Agents pick up the next signal automatically.'}
            </p>
            <button
              type="button"
              onClick={toggleWorker}
              className={`mt-2 flex w-full items-center justify-between rounded-lg border px-2.5 py-1.5 text-[11px] transition ${
                workerRunning
                  ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300'
                  : 'border-slate-700 text-slate-400 hover:border-slate-500'
              }`}
            >
              <span>Keep meeting 24/7</span>
              <span className="font-medium">{workerRunning ? 'ON' : 'OFF'}</span>
            </button>
          </div>

          {!loaded && <p className="px-2 text-sm text-slate-500">Loading the room…</p>}
          {loaded && !seats.length && (
            <p className="px-2 text-sm text-slate-500">
              No active agents. Seed the defaults from the Agents page to fill the table.
            </p>
          )}

          {seats.map((seat) => (
            <AgentPanel
              key={seat.role}
              seat={seat}
              focused={focusedRole === seat.role}
              onSelect={selectSeat}
            />
          ))}
        </aside>

        {/* ── Centre: the room ── */}
        <main className="relative min-h-[380px] flex-1 overflow-hidden rounded-2xl border border-amber-500/15 bg-[radial-gradient(120%_120%_at_50%_-10%,#12203a_0%,#0a1120_45%,#05070f_100%)] shadow-[inset_0_0_120px_-40px_rgba(56,189,248,0.25)]">
          <TradingRoomScene
            seats={sceneSeats}
            focusedRole={focusedRole}
            onSeatClick={selectSeat}
            screenInfo={screenInfo}
            quotes={boardQuotes}
            chartCandles={chartCandles}
            chartOverlays={chartOverlays}
            news={allNewsItems}
            speech={currentSpeech}
            cycleInfo={cycleInfo}
            paused={!visible}
            showFps
          />

          <div className="pointer-events-none absolute left-4 top-4 flex items-center gap-3 text-[11px]">
            <span
              className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 ${
                streamState === 'live' ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300'
              }`}
            >
              <Radio className="h-3 w-3" />
              {streamState === 'live' ? 'live' : streamState}
            </span>
            {focusSymbols.length > 0 && (
              <span className="rounded-full bg-cyan-500/15 px-2.5 py-1 font-mono text-cyan-300">
                focus: {focusSymbols.length === 1 ? focusSymbols[0] : `${focusSymbols.length} pairs`}
              </span>
            )}
            <span className="flex items-center gap-1.5 rounded-full bg-slate-800/70 px-2.5 py-1 text-slate-300">
              <Activity className="h-3 w-3" />
              {activeCount} working
            </span>
          </div>
        </main>

        {/* ── Right: focus + meeting log — scrollable, every card collapsible ── */}
        <aside className="flex w-full shrink-0 flex-col gap-3 overflow-y-auto overflow-x-hidden xl:w-96 min-h-0 pr-1 scrollbar-thin">
          <div className="shrink-0 overflow-hidden rounded-xl border border-slate-700/70 bg-slate-900/50">
            <button
              type="button"
              onClick={() => setPairsOpen((v) => !v)}
              className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition hover:bg-slate-800/50"
            >
              <Search className="h-3.5 w-3.5 shrink-0 text-cyan-400" />
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-300">Pair focus</span>
              <span className="ml-auto text-[10px] text-slate-500">
                {focusSymbols.length ? `${focusSymbols.length} pinned` : 'free roaming'}
              </span>
              {pairsOpen ? <ChevronUp className="h-3.5 w-3.5 shrink-0 text-slate-500" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-500" />}
            </button>
            {pairsOpen && (
              <div className="border-t border-slate-800 p-3">
                <PairFocusSelector focusSymbols={focusSymbols} onToggle={toggleFocus} onClear={clearFocus} />
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={() => setTaDeskOpen((v) => !v)}
            className={`flex shrink-0 items-center gap-2 rounded-xl border px-3 py-2.5 text-xs font-semibold transition ${
              taDeskOpen
                ? 'border-violet-400/60 bg-violet-500/20 text-violet-100'
                : 'border-violet-500/30 bg-violet-500/10 text-violet-300 hover:bg-violet-500/20'
            }`}
            title="TradingAgents multi-agent analysis desk — full pipeline on demand"
          >
            <Bot className="h-4 w-4" />
            TradingAgents desk
            <span className="ml-auto flex items-center gap-2 text-[10px] font-normal opacity-75">
              {taDeskOpen ? 'close' : 'open'}
              {taDeskOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            </span>
          </button>

          {taDeskOpen && (
            <div className="flex max-h-[60vh] min-h-[420px] shrink-0 flex-col overflow-hidden">
              <TradingAgentsDesk />
            </div>
          )}

          <DeskBrief symbol={chartSymbol} />

          <LiveDebate turns={debate} running={Boolean(runningSession)} />

          <div className="shrink-0">
            <AccountTabs />
          </div>
          {executions.length > 0 && (
            <div className="shrink-0 overflow-hidden rounded-xl border border-slate-700/70 bg-slate-900/50">
              <button
                type="button"
                onClick={() => setOrderDeskOpen((v) => !v)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-slate-800/40"
              >
                <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Order desk</span>
                <span className="ml-auto text-[10px] text-slate-500">{executions.length}</span>
                {orderDeskOpen ? <ChevronUp className="h-3.5 w-3.5 shrink-0 text-slate-500" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-500" />}
              </button>
              {orderDeskOpen && (
                <div className="border-t border-slate-800 p-3">
                  <div className="max-h-[28vh] space-y-1.5 overflow-y-auto pr-0.5">
                    {executions.slice(0, 5).map((e) => {
                      // Where it actually went. One decision can fill on several
                      // accounts at once — demo and live, or the exchange and the
                      // paper book — and which ones is the whole question a trader
                      // has when they see an order go out.
                      const legs = (e.order?.orders ?? [])
                        .filter((o) => o.status === 'placed')
                        .map((o) => o.role || o.venue)
                        .filter(Boolean)
                      return (
                        <div key={`${e.symbol}-${e.at}`} className="flex items-baseline gap-2 text-[11px]">
                          <span className={`rounded px-1.5 py-0.5 font-medium ${
                            e.status === 'placed' ? 'bg-emerald-500/20 text-emerald-300'
                            : e.status === 'skipped' ? 'bg-slate-600/40 text-slate-300'
                            : 'bg-red-500/20 text-red-300'
                          }`}>
                            {e.status === 'dry_run' ? 'DRY' : e.status.toUpperCase()}
                          </span>
                          <span className="font-mono text-slate-200">{e.symbol}</span>
                          <span className="uppercase text-slate-400">{e.action}</span>
                          {legs.length > 0 && (
                            <span className="rounded bg-cyan-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-cyan-300">
                              {legs.join(' · ')}
                            </span>
                          )}
                          <span className="truncate text-slate-500">{e.reason}</span>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="shrink-0 overflow-hidden rounded-xl border border-slate-700/70 bg-slate-900/50">
            <button
              type="button"
              onClick={() => setLogOpen((v) => !v)}
              className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-slate-800/40"
            >
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Meeting log</span>
              <span className="ml-auto text-[10px] text-slate-500">{sessions.length} sessions</span>
              {logOpen ? <ChevronUp className="h-3.5 w-3.5 shrink-0 text-slate-500" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-slate-500" />}
            </button>
            {logOpen && (
              <div className="max-h-[38vh] overflow-y-auto border-t border-slate-800 p-2">
                <SessionStream sessions={sessions} seats={seats} />
              </div>
            )}
          </div>
        </aside>
      </div>
    </>
  )
}
