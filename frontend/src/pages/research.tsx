import Head from 'next/head'
import { useEffect, useMemo, useState } from 'react'
import {
  addMonths,
  eachDayOfInterval,
  endOfMonth,
  endOfWeek,
  format,
  isSameMonth,
  parseISO,
  startOfMonth,
  startOfWeek,
  subMonths,
} from 'date-fns'
import {
  AlertTriangle,
  Brain,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Gauge,
  Loader2,
  Newspaper,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Telescope,
} from 'lucide-react'

import { apiClient } from '@/services/api'
import {
  CalendarEvent,
  ResearchFinding,
  ResearchKind,
  useResearchFeed,
} from '@/hooks/useResearchFeed'
import SignalResearchBoard from '@/components/research/SignalResearchBoard'
import { SOUTH_AFRICA_TIMEZONE, formatDateTimeZA } from '@/utils/datetime'

const POLL_MS = 60_000

/** The page's three jobs: read the findings, read the diary, watch the agents. */
type View = 'overview' | 'calendar' | 'signals'

const VIEWS: { id: View; label: string; hint: string }[] = [
  { id: 'overview', label: 'Overview', hint: 'Findings the background loop collected' },
  { id: 'calendar', label: 'Calendar', hint: 'Scheduled events and the agent reminder' },
  { id: 'signals', label: 'Signal Research', hint: 'Live per-pair research jobs' },
]

/** Day and time keys, both in the app's timezone, so the grid and the events
 *  it buckets can never disagree about which day an 01:30 release falls on. */
const ZA_DAY = new Intl.DateTimeFormat('en-CA', {
  timeZone: SOUTH_AFRICA_TIMEZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})
const ZA_TIME = new Intl.DateTimeFormat('en-ZA', {
  timeZone: SOUTH_AFRICA_TIMEZONE,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const dayKey = (iso: string) => ZA_DAY.format(new Date(iso))
const dayTime = (iso: string) => ZA_TIME.format(new Date(iso))

const IMPACT_DOT: Record<string, string> = {
  high: 'bg-red-400',
  medium: 'bg-amber-400',
  low: 'bg-gray-500',
  holiday: 'bg-blue-400',
}

const IMPACT_TEXT: Record<string, string> = {
  high: 'text-red-400',
  medium: 'text-amber-400',
  low: 'text-gray-400',
  holiday: 'text-blue-400',
}

const KIND_ICON: Record<ResearchKind, any> = {
  calendar: CalendarClock,
  news: Newspaper,
  sentiment: Gauge,
  prediction: Telescope,
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** "in 2d 4h" / "in 35m" / "12h ago" — a countdown, not a timestamp. */
function countdown(hours: number): string {
  const abs = Math.abs(hours)
  const suffix = hours < 0 ? ' ago' : ''
  const prefix = hours < 0 ? '' : 'in '
  if (abs < 1) return `${prefix}${Math.round(abs * 60)}m${suffix}`
  if (abs < 24) return `${prefix}${abs.toFixed(1)}h${suffix}`
  const days = Math.floor(abs / 24)
  return `${prefix}${days}d ${Math.round(abs - days * 24)}h${suffix}`
}

function decayIn(decayAt: string | null): string {
  if (!decayAt) return ''
  const hours = (new Date(decayAt).getTime() - Date.now()) / 3_600_000
  return hours <= 0 ? 'expired' : `decays ${countdown(hours)}`
}

export default function ResearchPage() {
  const [view, setView] = useState<View>('overview')
  const [fomoOnly, setFomoOnly] = useState(true)
  const [currency, setCurrency] = useState<string | null>(null)
  const [tab, setTab] = useState<'all' | ResearchKind>('all')
  // Anchored in an effect, not at render: "today" differs between the server
  // and the browser and would otherwise trip hydration.
  const [month, setMonth] = useState<Date | null>(null)
  const [selectedDay, setSelectedDay] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const { data, loading, error, refetch } = useResearchFeed({
    days: 30,
    fomo_only: fomoOnly,
    currency: currency || undefined,
  })

  useEffect(() => {
    const today = ZA_DAY.format(new Date())
    setMonth(startOfMonth(parseISO(today)))
    setSelectedDay(today)
  }, [])

  useEffect(() => {
    const id = setInterval(refetch, POLL_MS)
    return () => clearInterval(id)
  }, [refetch])

  const runCycle = async () => {
    setRunning(true)
    setActionError(null)
    try {
      await apiClient.research.run()
      await refetch()
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || err?.message || 'Research cycle failed')
    } finally {
      setRunning(false)
    }
  }

  const toggleLoop = async () => {
    setActionError(null)
    try {
      if (data.status?.running) await apiClient.research.stop()
      else await apiClient.research.start()
      await refetch()
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || err?.message || 'Could not change loop state')
    }
  }

  const eventsByDay = useMemo(() => {
    const map = new Map<string, CalendarEvent[]>()
    for (const event of data.events) {
      const key = dayKey(event.timestamp)
      const bucket = map.get(key)
      if (bucket) bucket.push(event)
      else map.set(key, [event])
    }
    return map
  }, [data.events])

  const gridDays = useMemo(() => {
    if (!month) return []
    return eachDayOfInterval({
      start: startOfWeek(startOfMonth(month), { weekStartsOn: 1 }),
      end: endOfWeek(endOfMonth(month), { weekStartsOn: 1 }),
    })
  }, [month])

  const visibleFindings = useMemo(
    () => (tab === 'all' ? data.findings : data.findings.filter((f) => f.kind === tab)),
    [data.findings, tab],
  )

  const selectedEvents = selectedDay ? eventsByDay.get(selectedDay) || [] : []
  const todayKey = month ? ZA_DAY.format(new Date()) : ''
  const verified = data.findings.filter((f) => !f.speculative).length
  const fearGreed = data.findings.find((f) => f.kind === 'sentiment')

  return (
    <>
      <Head><title>Research &amp; Calendar | TradeBot</title></Head>

      <div className="space-y-6">
        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <CalendarClock className="w-6 h-6 text-tradebot-accent" />
            <div>
              <h1 className="text-2xl font-bold text-white">Research &amp; Calendar</h1>
              <p className="text-sm text-gray-400">
                Background research, news/FOMO prediction, and the economic dates the agents are reminded of
              </p>
            </div>
          </div>

          {/* The loop controls drive the ambient research loop only. The signal
              queue has its own start/stop on its tab, so showing these there
              would be two different switches wearing the same label. */}
          <div className={`flex items-center gap-2 ${view === 'signals' ? 'hidden' : ''}`}>
            <span
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs border ${
                data.status?.running
                  ? 'bg-emerald-900/30 border-emerald-700/50 text-emerald-300'
                  : 'bg-gray-800/60 border-gray-700/50 text-gray-400'
              }`}
            >
              <span
                className={`w-2 h-2 rounded-full ${
                  data.status?.running ? 'bg-emerald-400 animate-pulse' : 'bg-gray-500'
                }`}
              />
              {data.status?.running
                ? `Loop running · every ${Math.round((data.status.interval_seconds || 900) / 60)}m`
                : 'Loop stopped'}
            </span>

            <button
              onClick={toggleLoop}
              className="flex items-center gap-2 px-3 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm text-white transition"
            >
              {data.status?.running ? <Square className="w-4 h-4" /> : <Play className="w-4 h-4" />}
              {data.status?.running ? 'Stop' : 'Start'}
            </button>

            <button
              onClick={runCycle}
              disabled={running}
              className="flex items-center gap-2 px-4 py-2 bg-tradebot-accent/20 hover:bg-tradebot-accent/30 disabled:bg-gray-800/60 disabled:text-gray-500 border border-tradebot-accent/40 rounded-lg text-sm text-tradebot-accent transition"
            >
              <RefreshCw className={`w-4 h-4 ${running ? 'animate-spin' : ''}`} />
              {running ? 'Researching…' : 'Run now'}
            </button>
          </div>
        </div>

        {(error || actionError) && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            {actionError || error}
          </div>
        )}

        {/* ── View tabs ──────────────────────────────────────────────────── */}
        <div className="flex flex-wrap gap-1 bg-gray-800/40 rounded-lg p-1 w-fit">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              onClick={() => setView(v.id)}
              title={v.hint}
              className={`px-4 py-2 rounded-md text-sm transition ${
                view === v.id
                  ? 'bg-tradebot-accent/20 text-tradebot-accent'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              {v.label}
            </button>
          ))}
        </div>

        {view === 'signals' && <SignalResearchBoard />}

        {view === 'overview' && (
        <>
        {/* ── Stats ──────────────────────────────────────────────────────── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard label="Active findings" value={data.findings.length}
            hint={`${verified} source-verified`} />
          <StatCard
            label="Unverified"
            value={data.findings.length - verified}
            hint="cannot gate a signal"
            tone={data.findings.length - verified > 0 ? 'amber' : undefined}
          />
          <StatCard
            label="Next high-impact event"
            value={data.nextEvent ? countdown(data.nextEvent.hours_away) : '—'}
            hint={data.nextEvent ? `${data.nextEvent.currency} · ${data.nextEvent.title}` : 'nothing scheduled'}
            tone={data.nextEvent && data.nextEvent.hours_away < 24 ? 'red' : undefined}
          />
          <StatCard
            label="Fear &amp; Greed"
            value={fearGreed ? fearGreed.headline.replace(/^.*?:\s*/, '') : '—'}
            hint={fearGreed ? 'the FOMO gauge' : 'no snapshot yet'}
          />
        </div>

        </>
        )}

        {view === 'calendar' && (
        <>
        {/* ── Calendar ───────────────────────────────────────────────────── */}
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <button
                onClick={() => month && setMonth(subMonths(month, 1))}
                className="p-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-white transition"
                aria-label="Previous month"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <h2 className="text-lg font-semibold text-white w-40 text-center">
                {month ? format(month, 'MMMM yyyy') : '—'}
              </h2>
              <button
                onClick={() => month && setMonth(addMonths(month, 1))}
                className="p-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-white transition"
                aria-label="Next month"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                onClick={() => setFomoOnly(!fomoOnly)}
                className={`px-3 py-1.5 rounded-lg text-xs border transition ${
                  fomoOnly
                    ? 'bg-red-500/20 border-red-500/40 text-red-300'
                    : 'bg-gray-800/60 border-gray-700/50 text-gray-400 hover:text-white'
                }`}
              >
                High impact only
              </button>
              <button
                onClick={() => setCurrency(null)}
                className={`px-3 py-1.5 rounded-lg text-xs border transition ${
                  currency === null
                    ? 'bg-tradebot-accent/15 border-tradebot-accent/40 text-tradebot-accent'
                    : 'bg-gray-800/60 border-gray-700/50 text-gray-400 hover:text-white'
                }`}
              >
                All
              </button>
              {data.currencies.map((code) => (
                <button
                  key={code}
                  onClick={() => setCurrency(code)}
                  className={`px-2.5 py-1.5 rounded-lg text-xs border transition ${
                    currency === code
                      ? 'bg-tradebot-accent/15 border-tradebot-accent/40 text-tradebot-accent'
                      : 'bg-gray-800/60 border-gray-700/50 text-gray-400 hover:text-white'
                  }`}
                >
                  {code}
                </button>
              ))}
            </div>
          </div>

          {!month ? (
            <div className="flex items-center justify-center py-20 text-gray-400">
              <Loader2 className="w-6 h-6 animate-spin" />
            </div>
          ) : (
            <>
              <div className="grid grid-cols-7 gap-1 text-center text-xs text-gray-500 uppercase tracking-wide">
                {WEEKDAYS.map((d) => <div key={d} className="py-1">{d}</div>)}
              </div>

              <div className="grid grid-cols-7 gap-1">
                {gridDays.map((day) => {
                  const key = format(day, 'yyyy-MM-dd')
                  const dayEvents = eventsByDay.get(key) || []
                  const hasFomo = dayEvents.some((e) => e.is_fomo)
                  const inMonth = isSameMonth(day, month)
                  const isToday = key === todayKey
                  const isSelected = key === selectedDay

                  return (
                    <button
                      key={key}
                      onClick={() => setSelectedDay(key)}
                      className={`min-h-[68px] p-1.5 rounded-lg border text-left transition ${
                        isSelected
                          ? 'bg-tradebot-accent/15 border-tradebot-accent/60'
                          : 'bg-gray-900/50 border-gray-700/40 hover:bg-gray-700/30'
                      } ${hasFomo && !isSelected ? 'ring-1 ring-red-500/40' : ''} ${
                        inMonth ? '' : 'opacity-35'
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span
                          className={`text-xs ${
                            isToday ? 'text-tradebot-accent font-bold' : 'text-gray-400'
                          }`}
                        >
                          {format(day, 'd')}
                        </span>
                        {dayEvents.length > 0 && (
                          <span className="text-[10px] text-gray-500">{dayEvents.length}</span>
                        )}
                      </div>
                      <div className="mt-1 space-y-0.5">
                        {dayEvents.slice(0, 3).map((e) => (
                          <div key={e.id} className="flex items-center gap-1">
                            <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${IMPACT_DOT[e.impact] || IMPACT_DOT.low}`} />
                            <span className="text-[10px] text-gray-400 truncate">{e.currency}</span>
                          </div>
                        ))}
                        {dayEvents.length > 3 && (
                          <div className="text-[10px] text-gray-600">+{dayEvents.length - 3}</div>
                        )}
                      </div>
                    </button>
                  )
                })}
              </div>

              {/* Selected day detail */}
              <div className="bg-gray-900/50 rounded-lg p-4 space-y-2">
                <h3 className="text-sm font-semibold text-white">
                  {selectedDay ? format(parseISO(selectedDay), 'EEEE, d MMMM yyyy') : 'Pick a day'}
                  <span className="ml-2 text-xs font-normal text-gray-500">
                    ({selectedEvents.length} event{selectedEvents.length === 1 ? '' : 's'})
                  </span>
                </h3>

                {selectedEvents.length === 0 ? (
                  <div className="text-center py-6 text-gray-500 text-sm">
                    No scheduled events on this day
                  </div>
                ) : (
                  <div className="space-y-1">
                    {selectedEvents.map((e) => (
                      <div
                        key={e.id}
                        className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 border-b border-gray-700/40 last:border-0"
                      >
                        <span className="text-xs text-gray-400 font-mono w-12">{dayTime(e.timestamp)}</span>
                        <span className="px-1.5 py-0.5 rounded bg-gray-800 text-[10px] text-gray-300 font-medium">
                          {e.currency}
                        </span>
                        <span className={`flex items-center gap-1 text-[10px] ${IMPACT_TEXT[e.impact] || IMPACT_TEXT.low}`}>
                          <span className={`w-1.5 h-1.5 rounded-full ${IMPACT_DOT[e.impact] || IMPACT_DOT.low}`} />
                          {e.impact}
                        </span>
                        <span className="text-sm text-white flex-1 min-w-[12rem]">{e.title}</span>
                        <span className="text-xs text-gray-500 space-x-3">
                          {e.actual != null && e.actual !== '' && (
                            <span className="text-tradebot-accent">actual {e.actual}</span>
                          )}
                          {e.forecast != null && e.forecast !== '' && <span>fc {e.forecast}</span>}
                          {e.previous != null && e.previous !== '' && <span>prev {e.previous}</span>}
                        </span>
                        <span className="text-[10px] text-gray-600">{countdown(e.hours_away)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* ── Agent reminder ─────────────────────────────────────────────── */}
        <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Brain className="w-5 h-5 text-purple-400" />
              <h2 className="text-lg font-semibold text-white">What the agents are told</h2>
            </div>
            <span className="text-xs text-gray-500">
              refreshed every research tick · {data.status?.last_run?.reminded ?? 0} row(s) last tick
            </span>
          </div>
          <p className="text-xs text-gray-400">
            Stored against <span className="font-mono text-gray-300">{data.reminder?.scope || 'all agents'}</span> at
            weight {data.reminder?.weight ?? 9}, so it is injected verbatim into every agent prompt.
          </p>
          <pre className="bg-gray-900/60 rounded-lg p-3 text-xs text-gray-300 whitespace-pre-wrap font-mono overflow-x-auto">
            {data.reminder?.content || 'No reminder written yet — run a research cycle.'}
          </pre>
        </div>
        </>
        )}

        {/* ── Findings ───────────────────────────────────────────────────── */}
        {view === 'overview' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex gap-1 bg-gray-800/40 rounded-lg p-1 w-fit">
              {(['all', 'calendar', 'news', 'sentiment', 'prediction'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-3 py-1.5 rounded-md text-sm capitalize transition ${
                    tab === t
                      ? 'bg-tradebot-accent/20 text-tradebot-accent'
                      : 'text-gray-400 hover:text-white'
                  }`}
                >
                  {t}
                  {t !== 'all' && (
                    <span className="ml-1.5 text-xs text-gray-500">
                      {data.findings.filter((f) => f.kind === t).length}
                    </span>
                  )}
                </button>
              ))}
            </div>
            {data.status?.last_run?.at && (
              <span className="text-xs text-gray-500">
                last cycle {formatDateTimeZA(data.status.last_run.at)}
              </span>
            )}
          </div>

          {loading && data.findings.length === 0 ? (
            <div className="flex items-center justify-center py-20 text-gray-400">
              <Loader2 className="w-6 h-6 animate-spin mr-2" /> Loading research…
            </div>
          ) : visibleFindings.length === 0 ? (
            <div className="text-center py-16 text-gray-500">
              No findings yet — run a research cycle to collect from the calendar, the news feeds and the Fear &amp; Greed index.
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {visibleFindings.map((f) => <FindingCard key={f.id} finding={f} />)}
            </div>
          )}
        </div>
        )}
      </div>
    </>
  )
}

function StatCard({
  label, value, hint, tone,
}: {
  label: string
  value: string | number
  hint?: string
  tone?: 'amber' | 'red'
}) {
  const valueColor =
    tone === 'red' ? 'text-red-400' : tone === 'amber' ? 'text-amber-400' : 'text-white'
  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4">
      <div className="text-xs text-gray-400">{label}</div>
      <div className={`text-xl font-bold mt-1 ${valueColor}`}>{value}</div>
      {hint && <div className="text-xs text-gray-500 mt-1 truncate">{hint}</div>}
    </div>
  )
}

function FindingCard({ finding }: { finding: ResearchFinding }) {
  const Icon = KIND_ICON[finding.kind] || Newspaper
  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4 space-y-2">
      <div className="flex items-start gap-2">
        <Icon className="w-4 h-4 text-tradebot-accent shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-white leading-snug">{finding.headline}</div>
          {finding.body && (
            <div className="text-xs text-gray-500 mt-1 line-clamp-2">{finding.body}</div>
          )}
        </div>
        {finding.symbol && (
          <span className="px-1.5 py-0.5 rounded bg-gray-900 text-[10px] text-gray-400 shrink-0">
            {finding.symbol}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        {finding.speculative ? (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded bg-amber-900/30 border border-amber-700/40 text-amber-300 text-[10px]">
            <AlertTriangle className="w-3 h-3" /> unverified — cannot gate a signal
          </span>
        ) : (
          <span className="flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-900/30 border border-emerald-700/40 text-emerald-300 text-[10px]">
            <ShieldCheck className="w-3 h-3" /> source-verified
          </span>
        )}
        {finding.source_url && (
          <a
            href={finding.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-gray-400 hover:text-tradebot-accent transition"
          >
            <ExternalLink className="w-3 h-3" />
            {finding.source || 'source'}
          </a>
        )}
        <span className="text-gray-600 ml-auto">{decayIn(finding.decay_at)}</span>
      </div>

      <div className="flex items-center gap-2">
        <div className="flex-1 h-1 bg-gray-900 rounded-full overflow-hidden">
          <div
            className="h-full bg-tradebot-accent/70"
            style={{ width: `${Math.min(100, Math.max(0, finding.confidence * 100))}%` }}
          />
        </div>
        <span className="text-[10px] text-gray-500 w-10 text-right">
          {(finding.confidence * 100).toFixed(0)}%
        </span>
      </div>
    </div>
  )
}
