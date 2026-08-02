import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';

export type JobStatus = 'queued' | 'researching' | 'done' | 'failed' | 'skipped';
export type JobSource = 'telegram' | 'sniper' | 'smc' | 'core' | 'manual';
export type Verdict = 'bullish' | 'bearish' | 'neutral' | 'stand_aside';

/** One entry in a job's pipeline log, as written by the backend mid-run. */
export interface ResearchStep {
  name: string;
  status: 'running' | 'done' | 'error' | 'empty';
  detail: string;
  ms: number;
}

/**
 * One costed entry plan. Research returns two per pair: `primary` is the setup
 * to take now or on the nearest retest, `secondary` the deeper/alternate fill.
 * Each carries its own stop and target — `rr` is recomputed server-side from
 * those levels, never taken from the model.
 */
export interface ResearchEntry {
  label: string;
  side: 'buy' | 'sell';
  entry: number;
  stop_loss: number;
  take_profit: number;
  rr: number | null;
  confidence: number;
  trigger: string;
  rationale: string;
}

/** One of the signals folded into a pair's batch. */
export interface BatchedSignal {
  source: string;
  signal_ref?: string;
  direction?: string | null;
  entry?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
}

/**
 * The reconciled view of one instrument: what every live signal on it adds up
 * to, plus the two entries. Shared by the sniper engines and every page that
 * lists pairs, so a pair's entries mean the same thing everywhere.
 */
export interface ResearchPlan {
  symbol: string;
  verdict: Verdict | null;
  confidence: number | null;
  horizon_hours: number | null;
  rationale: string | null;
  entries: ResearchEntry[];
  sources: string[];
  speculative: boolean;
  /** How many signals were reconciled into this plan. */
  signal_count: number;
  signal_sources: string;
  provider_used: string | null;
  job_id: number;
  researched_at: string;
  age_hours: number;
}

/**
 * A pair-research job. `progress` and `steps` are written back to the row as
 * each stage completes, so polling shows real movement rather than a spinner.
 */
export interface ResearchJob {
  id: number;
  symbol: string;
  /** "telegram+smc" when a pair's batch drew on several origins. */
  source: string;
  signal_ref: string;
  signal_refs: string[];
  /** Every signal folded into this run — the audit trail for the entries. */
  signals: BatchedSignal[];
  signal_count: number;
  entries: ResearchEntry[];
  /** Consensus direction, or null when the batch's signals disagree. */
  direction: string | null;
  entry: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  status: JobStatus;
  stage: string | null;
  progress: number;
  steps: ResearchStep[];
  verdict: Verdict | null;
  verdict_confidence: number | null;
  horizon_hours: number | null;
  rationale: string | null;
  sources: string[];
  /** True when nothing the prediction rests on had a resolvable source URL. */
  speculative: boolean;
  finding_id: number | null;
  provider_used: string | null;
  error: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface QueueStatus {
  running: boolean;
  concurrency: number;
  /** Jobs being researched right now, in this backend process. */
  active: number;
  scan_interval_seconds: number;
  started_at: string | null;
  last_scan: { at: string; queued?: number; error?: string } | null;
  queued: number;
  researching: number;
  done_24h: number;
  failed_24h: number;
  steps: string[];
  available?: boolean;
}

/** Poll fast while work is moving, slowly when the queue is idle. */
const ACTIVE_POLL_MS = 2_000;
const IDLE_POLL_MS = 15_000;

const isLive = (job: ResearchJob) =>
  job.status === 'queued' || job.status === 'researching';

/**
 * The Signal Research board's data: the job list plus the queue header.
 *
 * Mirrors `useResearchFeed` — both requests settle independently, so a failing
 * status endpoint still leaves the job list on screen.
 */
export function useResearchJobs(enabled = true) {
  const [jobs, setJobs] = useState<ResearchJob[]>([]);
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The poll interval depends on the data it fetches, so the fetch must not
  // depend on the interval — the rate lives in a ref and the timer re-arms
  // itself after each round instead.
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const fetchJobs = useCallback(async () => {
    if (!enabledRef.current) return [] as ResearchJob[];
    try {
      const [jobsRes, statusRes] = await Promise.allSettled([
        apiClient.research.jobs({ limit: 60 }),
        apiClient.research.queueStatus(),
      ]);

      let next: ResearchJob[] = [];
      if (jobsRes.status === 'fulfilled') {
        next = jobsRes.value.data.jobs || [];
        setJobs(next);
      }
      if (statusRes.status === 'fulfilled') setStatus(statusRes.value.data);

      setError(
        jobsRes.status === 'rejected' && statusRes.status === 'rejected'
          ? 'Research queue unreachable'
          : null,
      );
      return next;
    } catch (err: any) {
      setError(err?.response?.data?.detail || err?.message || 'Failed to load research jobs');
      return [] as ResearchJob[];
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;

    const round = async () => {
      const next = await fetchJobs();
      if (cancelled) return;
      timer = setTimeout(round, next.some(isLive) ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    };
    round();

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [enabled, fetchJobs]);

  return { jobs, status, loading, error, refetch: fetchJobs };
}
