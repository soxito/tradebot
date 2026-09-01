/** Shared types for the TradingAgents sidecar integration. */

export type TaPhase =
  | 'queued'
  | 'starting'
  | 'analysts'
  | 'research_debate'
  | 'research_manager'
  | 'trader'
  | 'risk_debate'
  | 'portfolio_manager'
  | 'done'
  | 'failed'

export interface TaRunSummary {
  run_id: string
  ticker: string
  mapped_ticker?: string | null
  trade_date: string
  source: string
  status: 'running' | 'done' | 'error'
  decision?: string | null
  confidence?: number | null
  reasoning?: string | null
  duration_s?: number | null
  created_at?: string | null
  finished_at?: string | null
  has_result?: boolean
}

export interface TaRunDetail extends TaRunSummary {
  result?: TaResult | null
  config_used?: Record<string, unknown> | null
  error?: string | null
}

export interface TaRecommendation {
  action?: string
  decision?: string
  confidence?: number | string
  reasoning?: string
  warning_message?: string
  [key: string]: unknown
}

export interface TaResult {
  ticker?: string
  trade_date?: string
  reports?: {
    market?: string
    sentiment?: string
    news?: string
    fundamentals?: string
  }
  situation_summary?: string
  investment_debate?: {
    bull_history?: string
    bear_history?: string
    judge_decision?: string
    turns?: number
  }
  trader_plan?: string
  risk_debate?: {
    aggressive_history?: string
    conservative_history?: string
    neutral_history?: string
    judge_decision?: string
    turns?: number
  }
  final_trade_decision?: string
  decision_summary?: string
  recommendation?: TaRecommendation
  message_count?: number
}

/** One streamed SSE event from the sidecar relay. */
export interface TaStreamEvent {
  seq: number
  type: 'start' | 'state' | 'message' | 'result' | 'done' | 'error'
  ts: string
  data: Record<string, unknown>
}

export const PHASE_ORDER: TaPhase[] = [
  'analysts',
  'research_debate',
  'research_manager',
  'trader',
  'risk_debate',
  'portfolio_manager',
]

export const PHASE_LABELS: Record<TaPhase, string> = {
  queued: 'Queued',
  starting: 'Starting',
  analysts: 'Analyst team',
  research_debate: 'Bull/Bear debate',
  research_manager: 'Research manager',
  trader: 'Trader',
  risk_debate: 'Risk debate',
  portfolio_manager: 'Portfolio manager',
  done: 'Decision',
  failed: 'Failed',
}
