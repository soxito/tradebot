/**
 * One research finding, as it appears on the /research feed.
 *
 * The body is a preview until it is opened. For a prediction that body is the
 * whole trade plan — both entries with their stops and targets, the
 * reconciliation, the sources — so clamping it with no way to expand hides the
 * numbers a person would act on. Its own file rather than a local function in
 * the page: the page pulls a dozen modules that a test of this card should not
 * have to load.
 */
import { useState } from 'react'
import {
  AlertTriangle,
  CalendarClock,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Gauge,
  Newspaper,
  ShieldCheck,
  Telescope,
} from 'lucide-react'

import type { ResearchFinding, ResearchKind } from '@/hooks/useResearchFeed'

const KIND_ICON: Record<ResearchKind, any> = {
  calendar: CalendarClock,
  news: Newspaper,
  sentiment: Gauge,
  prediction: Telescope,
}

function countdown(hours: number): string {
  const abs = Math.abs(hours)
  const suffix = hours < 0 ? ' ago' : ''
  const prefix = hours < 0 ? '' : 'in '
  if (abs < 1) return `${prefix}${Math.round(abs * 60)}m${suffix}`
  if (abs < 24) return `${prefix}${abs.toFixed(1)}h${suffix}`
  const days = Math.floor(abs / 24)
  return `${prefix}${days}d ${Math.round(abs - days * 24)}h${suffix}`
}

export function decayIn(decayAt: string | null): string {
  if (!decayAt) return ''
  const hours = (new Date(decayAt).getTime() - Date.now()) / 3_600_000
  return hours <= 0 ? 'expired' : `decays ${countdown(hours)}`
}

export default function FindingCard({ finding }: { finding: ResearchFinding }) {
  const Icon = KIND_ICON[finding.kind] || Newspaper
  const [open, setOpen] = useState(false)

  const body = finding.body || ''
  // Anything that would be cut off is worth a control; a one-line sentiment
  // note does not need one.
  const clamped = body.length > 140 || body.includes('\n')

  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-4 space-y-2">
      <div className="flex items-start gap-2">
        <Icon className="w-4 h-4 text-tradebot-accent shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-white leading-snug">{finding.headline}</div>
          {body && (
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              className="w-full text-left group"
              title={open ? 'Collapse' : 'Show the full research'}
            >
              <div
                className={`text-xs text-gray-500 mt-1 group-hover:text-gray-400 transition ${
                  open ? 'whitespace-pre-wrap' : 'line-clamp-2'
                }`}
              >
                {body}
              </div>
              {clamped && (
                <span className="inline-flex items-center gap-1 mt-1 text-[10px] text-tradebot-accent/80 group-hover:text-tradebot-accent">
                  {open ? (
                    <>
                      <ChevronUp className="w-3 h-3" /> show less
                    </>
                  ) : (
                    <>
                      <ChevronDown className="w-3 h-3" /> read full research
                    </>
                  )}
                </span>
              )}
            </button>
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
