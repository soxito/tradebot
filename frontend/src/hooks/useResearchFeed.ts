import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';

export type ResearchKind = 'calendar' | 'news' | 'sentiment' | 'prediction';
export type EventImpact = 'high' | 'medium' | 'low' | 'holiday';

/**
 * One piece of background research with its provenance. `speculative` is the
 * load-bearing flag: a finding with no resolvable source URL is stored and
 * shown, but can never gate a trade signal on its own.
 */
export interface ResearchFinding {
  id: number;
  kind: ResearchKind;
  symbol: string | null;
  headline: string;
  body: string | null;
  source: string | null;
  source_url: string | null;
  confidence: number;
  speculative: boolean;
  provider_used: string | null;
  published_at: string | null;
  decay_at: string | null;
  created_at: string | null;
}

/**
 * A scheduled economic release. `is_fomo` marks the high-impact ones — the
 * dates that actually move price, and what the agent reminder is built from.
 */
export interface CalendarEvent {
  id: string;
  title: string;
  currency: string;
  impact: EventImpact;
  /** ISO 8601, UTC. */
  timestamp: string;
  /** YYYY-MM-DD, UTC — the calendar grid keys off this. */
  date: string;
  time_utc: string;
  hours_away: number;
  forecast: string | number | null;
  previous: string | number | null;
  actual: string | number | null;
  is_fomo: boolean;
  source: string;
}

export interface ResearchStatus {
  running: boolean;
  interval_seconds?: number;
  started_at?: string | null;
  available?: boolean;
  last_run?: {
    collected?: number;
    stored?: number;
    verified?: number;
    speculative?: number;
    proposals?: number;
    reminded?: number;
    llm_step?: string;
    idle_providers?: string[];
    at?: string;
    status?: string;
    error?: string;
  } | null;
}

export interface AgentReminder {
  title: string;
  /** Verbatim text injected into every agent prompt. */
  content: string;
  events: CalendarEvent[];
  scope: string;
  weight: number;
  source: string;
  available: boolean;
}

export interface CalendarParams {
  days?: number;
  currency?: string;
  impact?: string;
  fomo_only?: boolean;
}

export interface ResearchFeed {
  findings: ResearchFinding[];
  events: CalendarEvent[];
  /** Every currency in the unfiltered window, for the filter chips. */
  currencies: string[];
  nextEvent: CalendarEvent | null;
  status: ResearchStatus | null;
  reminder: AgentReminder | null;
}

const EMPTY: ResearchFeed = {
  findings: [],
  events: [],
  currencies: [],
  nextEvent: null,
  status: null,
  reminder: null,
};

/**
 * Everything the /research page renders, in one poll: the calendar window, the
 * undecayed findings, the loop status and the block the agents are being fed.
 *
 * Each request settles independently — a dead news feed still leaves the
 * calendar on screen, which mirrors how the research loop itself behaves.
 */
export function useResearchFeed(params: CalendarParams = {}, enabled = true) {
  const [data, setData] = useState<ResearchFeed>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Stabilise the params object across renders
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const fetchFeed = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    try {
      const [calendarRes, findingsRes, statusRes, reminderRes] = await Promise.allSettled([
        apiClient.research.calendar(paramsRef.current),
        apiClient.research.findings({ limit: 120 }),
        apiClient.research.status(),
        apiClient.research.reminder(),
      ]);

      setData((prev) => ({
        events:
          calendarRes.status === 'fulfilled'
            ? calendarRes.value.data.events || []
            : prev.events,
        currencies:
          calendarRes.status === 'fulfilled'
            ? calendarRes.value.data.currencies || []
            : prev.currencies,
        nextEvent:
          calendarRes.status === 'fulfilled'
            ? calendarRes.value.data.next_event || null
            : prev.nextEvent,
        findings:
          findingsRes.status === 'fulfilled'
            ? findingsRes.value.data.findings || []
            : prev.findings,
        status: statusRes.status === 'fulfilled' ? statusRes.value.data : prev.status,
        reminder:
          reminderRes.status === 'fulfilled' ? reminderRes.value.data : prev.reminder,
      }));

      // Only a total failure is worth surfacing — one dead endpoint is not.
      const allFailed = [calendarRes, findingsRes, statusRes, reminderRes].every(
        (r) => r.status === 'rejected',
      );
      setError(allFailed ? 'Research API unreachable' : null);
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to load research');
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    fetchFeed();
  }, [fetchFeed, params.days, params.currency, params.impact, params.fomo_only]);

  return { data, loading, error, refetch: fetchFeed };
}
