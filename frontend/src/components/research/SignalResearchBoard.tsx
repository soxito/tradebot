import { useMemo, useState } from 'react'
import {
  AlertTriangle,
  Loader2,
  Play,
  RadioTower,
  Search,
  Square,
} from 'lucide-react'

import { apiClient } from '@/services/api'
import { useResearchJobs } from '@/hooks/useResearchJobs'
import ResearchJobCard from './ResearchJobCard'

/** Fallback order if the backend has not reported its pipeline yet. */
const DEFAULT_STEPS = [
  'load_signal', 'price_history', 'pair_knowledge',
  'stored_research', 'web_news', 'calendar', 'predict',
]

/**
 * The Signal Research board.
 *
 * Every signal the app creates or ingests — Telegram, sniper, SMC, core — is
 * researched into a prediction here. The queue works a bounded number of pairs
 * at a time (the "slots" counter), and each in-flight card shows which of the
 * seven research steps has landed so a long run is legible rather than opaque.
 */
export default function SignalResearchBoard() {
  const { jobs, status, loading, error, refetch } = useResearchJobs()
  const [busy, setBusy] = useState<'scan' | 'toggle' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const steps = status?.steps?.length ? status.steps : DEFAULT_STEPS
  const { live, researching, finished } = useMemo(() => ({
    live: jobs.filter((j) => j.status === 'queued' || j.status === 'researching'),
    // Only `researching` counts against the concurrency cap — a queued job is
    // waiting for a slot, not occupying one.
    researching: jobs.filter((j) => j.status === 'researching').length,
    finished: jobs.filter((j) => j.status !== 'queued' && j.status !== 'researching'),
  }), [jobs])

  const run = async (kind: 'scan' | 'toggle', fn: () => Promise<unknown>) => {
    setBusy(kind)
    setActionError(null)
    try {
      await fn()
      await refetch()
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || err?.message || 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  const toggleQueue = () =>
    run('toggle', () =>
      status?.running ? apiClient.research.queueStop() : apiClient.research.queueStart(),
    )

  return (
    <div className="space-y-4">
      {/* ── Queue header ───────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-gray-800/60 border border-gray-700/50 rounded-xl p-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <RadioTower className="w-5 h-5 text-tradebot-accent" />
            <div>
              <div className="text-sm font-semibold text-white">Signal research queue</div>
              <div className="text-xs text-gray-500">
                Telegram, sniper, SMC and app signals — researched into predictions
              </div>
            </div>
          </div>

          <span
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs border ${
              status?.running
                ? 'bg-emerald-900/30 border-emerald-700/50 text-emerald-300'
                : 'bg-gray-800/60 border-gray-700/50 text-gray-400'
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                status?.running ? 'bg-emerald-400 animate-pulse' : 'bg-gray-500'
              }`}
            />
            {status?.running ? 'Queue running' : 'Queue stopped'}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <Stat label="Slots in use" value={`${status?.active ?? 0} / ${status?.concurrency ?? 5}`} />
          <Stat label="Queued" value={status?.queued ?? 0} />
          <Stat label="Done (24h)" value={status?.done_24h ?? 0} />
          <Stat label="Failed (24h)" value={status?.failed_24h ?? 0}
            tone={(status?.failed_24h ?? 0) > 0 ? 'red' : undefined} />

          <div className="flex items-center gap-2">
            <button
              onClick={toggleQueue}
              disabled={busy !== null}
              className="flex items-center gap-2 px-3 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm text-white transition"
            >
              {status?.running ? <Square className="w-4 h-4" /> : <Play className="w-4 h-4" />}
              {status?.running ? 'Stop' : 'Start'}
            </button>
            <button
              onClick={() => run('scan', () => apiClient.research.scan())}
              disabled={busy !== null}
              className="flex items-center gap-2 px-4 py-2 bg-tradebot-accent/20 hover:bg-tradebot-accent/30 disabled:bg-gray-800/60 disabled:text-gray-500 border border-tradebot-accent/40 rounded-lg text-sm text-tradebot-accent transition"
            >
              {busy === 'scan'
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <Search className="w-4 h-4" />}
              {busy === 'scan' ? 'Scanning…' : 'Scan signals'}
            </button>
          </div>
        </div>
      </div>

      {(error || actionError) && (
        <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          {actionError || error}
        </div>
      )}

      {/* ── In flight ──────────────────────────────────────────────────── */}
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-white flex items-center gap-2">
          Researching now
          <span className="text-xs font-normal text-gray-500">
            {researching} in flight · max {status?.concurrency ?? 5} at a time
            {live.length - researching > 0 && ` · ${live.length - researching} waiting`}
          </span>
        </h3>

        {loading && jobs.length === 0 ? (
          <div className="flex items-center justify-center py-16 text-gray-400">
            <Loader2 className="w-6 h-6 animate-spin mr-2" /> Loading queue…
          </div>
        ) : live.length === 0 ? (
          <div className="text-center py-10 text-gray-500 text-sm border border-dashed border-gray-700/50 rounded-xl">
            Nothing in the queue. Hit <span className="text-gray-300">Scan signals</span> to sweep
            Telegram, the snipers and the app&apos;s own signals for work.
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {live.map((job) => (
              <ResearchJobCard key={job.id} job={job} allSteps={steps} />
            ))}
          </div>
        )}
      </section>

      {/* ── Completed ──────────────────────────────────────────────────── */}
      {finished.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-sm font-semibold text-white">
            Completed
            <span className="ml-2 text-xs font-normal text-gray-500">{finished.length}</span>
          </h3>
          <div className="grid gap-3 lg:grid-cols-2">
            {finished.map((job) => (
              <ResearchJobCard key={job.id} job={job} allSteps={steps} />
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function Stat({
  label, value, tone,
}: {
  label: string
  value: string | number
  tone?: 'red'
}) {
  return (
    <div>
      <div className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</div>
      <div className={`text-sm font-semibold ${tone === 'red' ? 'text-red-400' : 'text-white'}`}>
        {value}
      </div>
    </div>
  )
}
