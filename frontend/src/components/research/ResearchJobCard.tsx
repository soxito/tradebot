import {
  AlertTriangle,
  Check,
  CircleDashed,
  ExternalLink,
  Layers,
  Loader2,
  Minus,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react'

import type { ResearchJob, Verdict } from '@/hooks/useResearchJobs'

/** Human labels for the backend's pipeline step names. */
const STEP_LABEL: Record<string, string> = {
  load_signal: 'Reconcile the pair’s signals',
  price_history: 'Price movement (1h/4h/1d)',
  pair_knowledge: 'What we know about the pair',
  stored_research: 'Research already on file',
  web_news: 'Search the web + provider feeds',
  calendar: 'Scheduled events',
  predict: 'Write the prediction',
}

const VERDICT_STYLE: Record<Verdict, { cls: string; Icon: any; label: string }> = {
  bullish:      { cls: 'text-emerald-300 border-emerald-700/40 bg-emerald-900/30', Icon: TrendingUp,   label: 'Bullish' },
  bearish:      { cls: 'text-red-300 border-red-700/40 bg-red-900/30',             Icon: TrendingDown, label: 'Bearish' },
  neutral:      { cls: 'text-gray-300 border-gray-600/40 bg-gray-800/60',          Icon: Minus,        label: 'Neutral' },
  stand_aside:  { cls: 'text-amber-300 border-amber-700/40 bg-amber-900/30',       Icon: AlertTriangle, label: 'Stand aside' },
}

const SOURCE_LABEL: Record<string, string> = {
  telegram: 'Telegram', sniper: 'Sniper', smc: 'SMC', core: 'Signal', manual: 'Manual',
}

function StepIcon({ status }: { status: string }) {
  if (status === 'running') return <Loader2 className="w-3.5 h-3.5 text-tradebot-accent animate-spin shrink-0" />
  if (status === 'done') return <Check className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
  if (status === 'error') return <X className="w-3.5 h-3.5 text-red-400 shrink-0" />
  return <CircleDashed className="w-3.5 h-3.5 text-gray-600 shrink-0" />
}

/**
 * One pair's research run: what the agents are doing right now, which of the
 * seven steps have landed, and — once the model has answered — the prediction
 * with the sources it actually rests on.
 */
export default function ResearchJobCard({
  job,
  allSteps,
}: {
  job: ResearchJob
  /** The full pipeline, so steps that have not started yet still render. */
  allSteps: string[]
}) {
  const live = job.status === 'queued' || job.status === 'researching'
  const byName = new Map(job.steps.map((s) => [s.name, s]))
  const verdict = job.verdict ? VERDICT_STYLE[job.verdict] : null

  return (
    <div
      className={`bg-gray-800/60 border rounded-xl p-4 space-y-3 ${
        job.status === 'failed' ? 'border-red-800/50' : 'border-gray-700/50'
      }`}
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-semibold text-white">{job.symbol}</span>
            {/* Sources are joined with "+" when a pair's batch drew on several. */}
            {job.source.split('+').map((s) => (
              <span key={s} className="px-1.5 py-0.5 rounded bg-gray-900 text-[10px] text-gray-400">
                {SOURCE_LABEL[s] || s}
              </span>
            ))}
            {job.signal_count > 1 && (
              <span
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-tradebot-accent/15 text-[10px] text-tradebot-accent"
                title="Every live signal on this pair is researched together, not one at a time"
              >
                <Layers className="w-3 h-3" />
                {job.signal_count} signals combined
              </span>
            )}
            {job.direction ? (
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] ${
                  job.direction === 'buy'
                    ? 'bg-emerald-900/40 text-emerald-300'
                    : 'bg-red-900/40 text-red-300'
                }`}
              >
                {job.direction.toUpperCase()}
              </span>
            ) : job.signal_count > 1 && (
              <span
                className="px-1.5 py-0.5 rounded bg-amber-900/30 text-[10px] text-amber-300"
                title="The signals disagree on direction — resolving that is part of the research"
              >
                SPLIT
              </span>
            )}
          </div>

          {/* The signals that went in, so the entries below have an audit trail. */}
          {job.signals.length > 1 ? (
            <div className="text-[11px] text-gray-500 mt-1 space-y-0.5">
              {job.signals.slice(0, 4).map((s, i) => (
                <div key={s.signal_ref || i}>
                  <span className="text-gray-600">{s.source}</span>{' '}
                  <span className={s.direction === 'buy' ? 'text-emerald-400/80' : 'text-red-400/80'}>
                    {String(s.direction || '?').toUpperCase()}
                  </span>
                  {s.entry != null && <> @ {s.entry}</>}
                  {s.stop_loss != null && <> · SL {s.stop_loss}</>}
                  {s.take_profit != null && <> · TP {s.take_profit}</>}
                </div>
              ))}
              {job.signals.length > 4 && (
                <div className="text-gray-600">+{job.signals.length - 4} more</div>
              )}
            </div>
          ) : (
            <div className="text-xs text-gray-500 mt-0.5">
              {job.entry != null && <>entry {job.entry}</>}
              {job.stop_loss != null && <> · stop {job.stop_loss}</>}
              {job.take_profit != null && <> · target {job.take_profit}</>}
            </div>
          )}
        </div>

        {live ? (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg text-[10px] bg-tradebot-accent/15 border border-tradebot-accent/40 text-tradebot-accent shrink-0">
            <Loader2 className="w-3 h-3 animate-spin" />
            {job.status === 'queued' ? 'Queued' : STEP_LABEL[job.stage || ''] || job.stage}
          </span>
        ) : verdict ? (
          <span className={`flex items-center gap-1.5 px-2 py-1 rounded-lg text-[10px] border shrink-0 ${verdict.cls}`}>
            <verdict.Icon className="w-3 h-3" />
            {verdict.label}
            {job.verdict_confidence != null && ` ${(job.verdict_confidence * 100).toFixed(0)}%`}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg text-[10px] bg-red-900/30 border border-red-800/50 text-red-300 shrink-0">
            <X className="w-3 h-3" /> Failed
          </span>
        )}
      </div>

      {/* ── Progress ───────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <div className="flex-1 h-1.5 bg-gray-900 rounded-full overflow-hidden">
          <div
            className={`h-full transition-all duration-500 ${
              job.status === 'failed' ? 'bg-red-500/70' : 'bg-tradebot-accent/70'
            }`}
            style={{ width: `${Math.min(100, Math.max(0, job.progress * 100))}%` }}
          />
        </div>
        <span className="text-[10px] text-gray-500 w-10 text-right">
          {(job.progress * 100).toFixed(0)}%
        </span>
      </div>

      {/* ── Step checklist ─────────────────────────────────────────────── */}
      <div className="space-y-1">
        {allSteps.map((name) => {
          const step = byName.get(name)
          return (
            <div key={name} className="flex items-center gap-2 text-xs">
              <StepIcon status={step?.status || 'pending'} />
              <span className={step ? 'text-gray-300' : 'text-gray-600'}>
                {STEP_LABEL[name] || name}
              </span>
              {step?.detail && (
                <span className="text-gray-600 truncate flex-1 min-w-0">— {step.detail}</span>
              )}
              {step && step.ms > 0 && (
                <span className="text-[10px] text-gray-600 ml-auto shrink-0">
                  {step.ms >= 1000 ? `${(step.ms / 1000).toFixed(1)}s` : `${step.ms}ms`}
                </span>
              )}
            </div>
          )
        })}
      </div>

      {/* ── The deliverable: two costed entries ────────────────────────── */}
      {job.entries.length > 0 && (
        <div className="border-t border-gray-700/40 pt-3 space-y-1">
          <div className="text-[10px] uppercase tracking-wide text-gray-500">
            Researched entries
          </div>
          {job.entries.map((e) => {
            const buy = e.side === 'buy'
            return (
              <div key={e.label} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <span className="text-[10px] uppercase tracking-wide text-gray-500 w-16 shrink-0">
                  {e.label}
                </span>
                <span
                  className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                    buy ? 'bg-emerald-900/40 text-emerald-300' : 'bg-red-900/40 text-red-300'
                  }`}
                >
                  {e.side.toUpperCase()}
                </span>
                <span className="text-xs text-white font-mono">{e.entry}</span>
                <span className="text-[11px] text-red-400/90 font-mono">SL {e.stop_loss}</span>
                <span className="text-[11px] text-emerald-400/90 font-mono">TP {e.take_profit}</span>
                {e.rr != null && (
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded ${
                      e.rr >= 2 ? 'bg-emerald-900/30 text-emerald-300' : 'bg-gray-800 text-gray-400'
                    }`}
                  >
                    {e.rr}R
                  </span>
                )}
                <span className="text-[10px] text-gray-500 ml-auto">
                  {(e.confidence * 100).toFixed(0)}%
                </span>
                {e.trigger && (
                  <div className="w-full text-[10px] text-gray-500 pl-16">↳ {e.trigger}</div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* ── Outcome ────────────────────────────────────────────────────── */}
      {job.rationale && (
        <p className="text-xs text-gray-400 leading-relaxed border-t border-gray-700/40 pt-3">
          {job.rationale}
        </p>
      )}

      {job.error && (
        <p className="text-xs text-red-400/90 border-t border-gray-700/40 pt-3 break-words">
          {job.error}
        </p>
      )}

      {job.status === 'done' && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {job.speculative ? (
            <span className="flex items-center gap-1 px-2 py-0.5 rounded bg-amber-900/30 border border-amber-700/40 text-amber-300 text-[10px]">
              <AlertTriangle className="w-3 h-3" /> unverified — cannot gate a signal
            </span>
          ) : (
            <span className="flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-900/30 border border-emerald-700/40 text-emerald-300 text-[10px]">
              <ShieldCheck className="w-3 h-3" /> source-verified
            </span>
          )}
          {job.horizon_hours != null && (
            <span className="text-gray-500 text-[10px]">{job.horizon_hours}h horizon</span>
          )}
          {job.provider_used && (
            <span className="text-gray-600 text-[10px]">via {job.provider_used}</span>
          )}
          {job.sources.slice(0, 3).map((url) => (
            <a
              key={url}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-gray-400 hover:text-tradebot-accent transition text-[10px]"
            >
              <ExternalLink className="w-3 h-3" />
              {(() => {
                try { return new URL(url).hostname.replace(/^www\./, '') } catch { return 'source' }
              })()}
            </a>
          ))}
        </div>
      )}
    </div>
  )
}
