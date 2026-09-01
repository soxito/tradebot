/**
 * TradingAgentsPanel — the multi-agent desk inside the Trading Room.
 *
 * Convenes the upstream TradingAgents framework (analysts → bull/bear debate →
 * trader → risk debate → portfolio manager) on any ticker, streams the whole
 * pipeline live over SSE and keeps a durable history. Runs execute in the
 * sidecar service; this panel only ever talks to the main backend.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, History, Radio } from 'lucide-react'

import { apiClient } from '@/services/api'
import PipelinePanel from './PipelinePanel'
import ReportViewer from './ReportViewer'
import RunForm, { type RunOptions } from './RunForm'
import RunsHistory from './RunsHistory'
import type { TaPhase, TaResult, TaStreamEvent } from './types'

type Status = 'idle' | 'running' | 'done' | 'error'

export default function TradingAgentsDesk() {
  const [tab, setTab] = useState<'desk' | 'history'>('desk')
  const [sidecarOk, setSidecarOk] = useState<boolean | null>(null)

  const [runId, setRunId] = useState<string | null>(null)
  const [phase, setPhase] = useState<TaPhase | null>(null)
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<string | null>(null)
  const [events, setEvents] = useState<TaStreamEvent[]>([])
  const [result, setResult] = useState<TaResult | null>(null)
  const [historyKey, setHistoryKey] = useState(0)

  const esRef = useRef<EventSource | null>(null)
  // Mirror of `status` so the EventSource error handler reads current state
  // without being recreated on every change.
  const statusRef = useRef<Status>('idle')
  useEffect(() => {
    statusRef.current = status
  }, [status])

  // Health probe — cheap, and tells the user before they pay for a doomed run.
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const res = await apiClient.tradingAgents.status()
        if (alive) setSidecarOk(Boolean(res.data?.ok))
      } catch {
        if (alive) setSidecarOk(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  const closeStream = useCallback(() => {
    esRef.current?.close()
    esRef.current = null
  }, [])

  useEffect(() => closeStream, [closeStream])

  const loadFullRun = useCallback(async (id: string) => {
    try {
      const res = await apiClient.tradingAgents.getRun(id)
      if (res.data?.result) setResult(res.data.result as TaResult)
      setHistoryKey((k) => k + 1)
    } catch {
      /* the streamed result event usually already carried everything */
    }
  }, [])

  const openStream = useCallback(
    (id: string) => {
      closeStream()
      setEvents([])
      setResult(null)
      setError(null)
      setStatus('running')
      setPhase('starting')

      const es = new EventSource(apiClient.tradingAgents.streamUrl(id))
      esRef.current = es

      const push = (type: TaStreamEvent['type'], raw: string) => {
        try {
          const parsed = JSON.parse(raw) as TaStreamEvent
          setEvents((prev) => [...prev.slice(-499), { ...parsed, type }])
        } catch {
          /* ignore malformed frames */
        }
      }

      es.addEventListener('start', (e) => push('start', (e as MessageEvent).data))
      es.addEventListener('state', (e) => {
        push('state', (e as MessageEvent).data)
        try {
          const data = JSON.parse((e as MessageEvent).data)?.data
          if (data?.phase) setPhase(data.phase as TaPhase)
        } catch { /* phase stays where it was */ }
      })
      es.addEventListener('message', (e) => push('message', (e as MessageEvent).data))
      es.addEventListener('result', (e) => {
        push('result', (e as MessageEvent).data)
        try {
          const payload = JSON.parse((e as MessageEvent).data)?.data
          if (payload && typeof payload === 'object') setResult(payload as TaResult)
        } catch { /* full fetch below covers it */ }
      })
      es.addEventListener('done', () => {
        setPhase('done')
        setStatus('done')
        void loadFullRun(id)
        closeStream()
      })
      es.addEventListener('end', () => {
        // Server closed the feed; only treat it as failure if no verdict arrived.
        if (statusRef.current === 'running') {
          setPhase('failed')
          setStatus('error')
          setError('stream ended before a decision was reached')
          setHistoryKey((k) => k + 1)
        }
        closeStream()
      })
      es.addEventListener('error', (e) => {
        const msg =
          e instanceof MessageEvent && e.data
            ? String(JSON.parse(e.data)?.error ?? 'run failed')
            : 'run failed'
        setError(msg)
        setPhase('failed')
        setStatus('error')
        setHistoryKey((k) => k + 1)
      })
      es.onerror = () => {
        // Terminal events arrive before the server closes; only surface an
        // error when we never reached a decision.
        if (statusRef.current === 'running') {
          setStatus('error')
          setError('connection lost')
        }
      }
    },
    [closeStream, loadFullRun],
  )

  const startRun = useCallback(
    async (opts: RunOptions) => {
      setTab('desk')
      setStatus('running')
      setPhase('queued')
      setEvents([])
      setResult(null)
      setError(null)
      try {
        const res = await apiClient.tradingAgents.analyze({
          ticker: opts.ticker,
          trade_date: opts.trade_date || undefined,
          llm_provider: opts.llm_provider,
          deep_think_llm: opts.deep_think_llm || undefined,
          quick_think_llm: opts.quick_think_llm || undefined,
          reasoning_effort: opts.reasoning_effort,
          max_debate_rounds: opts.max_debate_rounds,
          max_risk_discuss_rounds: opts.max_risk_discuss_rounds,
        })
        const id = res.data?.run_id
        if (!id) throw new Error(res.data?.detail ?? 'no run id returned')
        setRunId(id)
        setHistoryKey((k) => k + 1)
        openStream(id)
      } catch (err) {
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        setError(detail ?? String(err))
        setStatus('error')
        setPhase('failed')
      }
    },
    [openStream],
  )

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-violet-500/25 bg-slate-900/50">
      {/* Header */}
      <div className="flex shrink-0 items-center gap-2 border-b border-slate-800 px-3 py-2">
        <Bot className="h-4 w-4 text-violet-400" />
        <span className="text-xs font-semibold text-violet-200">TradingAgents desk</span>
        <span
          className={`ml-auto flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] ${
            sidecarOk === null
              ? 'bg-slate-700/50 text-slate-400'
              : sidecarOk
                ? 'bg-emerald-500/15 text-emerald-300'
                : 'bg-red-500/15 text-red-300'
          }`}
        >
          <Radio className="h-3 w-3" />
          {sidecarOk === null ? 'checking' : sidecarOk ? 'sidecar live' : 'sidecar offline'}
        </span>
      </div>

      {/* Tabs */}
      <div className="flex shrink-0 gap-1 border-b border-slate-800 px-2 pt-1.5">
        {(
          [
            ['desk', 'New analysis'],
            ['history', 'History'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`rounded-t-lg px-3 py-1.5 text-[11px] font-medium transition ${
              tab === key ? 'bg-slate-800/80 text-cyan-200' : 'text-slate-500 hover:text-slate-300'
            }`}
          >
            {label}
            {key === 'history' && <History className="mb-0.5 ml-1 inline h-3 w-3" />}
          </button>
        ))}
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {sidecarOk === false && tab === 'desk' && (
          <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-200">
            The TradingAgents sidecar isn&apos;t reachable. It starts automatically with the backend —
            check <span className="font-mono">integrations/TradingAgents/.venv</span> is set up.
          </p>
        )}

        {tab === 'desk' ? (
          <>
            <RunForm running={status === 'running'} onRun={startRun} />
            <PipelinePanel
              runId={runId}
              phase={phase}
              status={status}
              error={error}
              events={events}
            />
            {(status === 'done' || result) && <ReportViewer result={result} />}
          </>
        ) : (
          <RunsHistory
            refreshKey={historyKey}
            selectedRunId={runId}
            onSelect={(id) => {
              setTab('desk')
              setRunId(id)
              setStatus('done')
              setPhase('done')
              setEvents([])
              void loadFullRun(id)
            }}
          />
        )}
      </div>
    </div>
  )
}
