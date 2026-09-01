/**
 * RunForm — launch a TradingAgents analysis.
 *
 * Ticker + date plus the knobs that map 1:1 onto the sidecar's
 * TradingAgentsConfig. Defaults mirror the backend so pressing Run with a
 * bare ticker does the expected deep analysis.
 */
import { useEffect, useState } from 'react'
import { Loader2, Play } from 'lucide-react'

import { apiClient } from '@/services/api'

export interface RunOptions {
  ticker: string
  trade_date: string
  llm_provider: string
  deep_think_llm: string
  quick_think_llm: string
  reasoning_effort: string
  max_debate_rounds: number
  max_risk_discuss_rounds: number
}

const PROVIDERS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google_genai', label: 'Google' },
  { value: 'xai', label: 'xAI' },
  { value: 'ollama', label: 'Ollama (local)' },
]

const EFFORTS = ['low', 'medium', 'high']

function todayISO(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

interface Props {
  running: boolean
  onRun: (opts: RunOptions) => void
}

export default function RunForm({ running, onRun }: Props) {
  const [ticker, setTicker] = useState('BTC/USDT')
  const [tradeDate, setTradeDate] = useState(todayISO())
  const [provider, setProvider] = useState('openai')
  const [keyedProviders, setKeyedProviders] = useState<Record<string, boolean>>({})
  const [deepModel, setDeepModel] = useState('')
  const [quickModel, setQuickModel] = useState('')
  const [effort, setEffort] = useState('medium')
  const [debateRounds, setDebateRounds] = useState(2)
  const [riskRounds, setRiskRounds] = useState(2)
  const [advanced, setAdvanced] = useState(false)

  // Ask the sidecar which provider keys exist and default to one that will
  // actually work — a desk that fails on its first click teaches nobody anything.
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const res = await apiClient.tradingAgents.providers()
        const map: Record<string, boolean> = res.data?.providers ?? {}
        if (!alive) return
        setKeyedProviders(map)
        if (map[provider] === false) {
          const firstOk = Object.entries(map).find(([, ok]) => ok)?.[0]
          if (firstOk) setProvider(firstOk)
        }
      } catch {
        /* dropdown keeps the default; errors surface at run time */
      }
    })()
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submit = () => {
    if (!ticker.trim() || running) return
    onRun({
      ticker: ticker.trim(),
      trade_date: tradeDate,
      llm_provider: provider,
      deep_think_llm: deepModel.trim(),
      quick_think_llm: quickModel.trim(),
      reasoning_effort: effort,
      max_debate_rounds: debateRounds,
      max_risk_discuss_rounds: riskRounds,
    })
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
          placeholder="AAPL · BTC/USDT · 0700.HK"
          className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950/70 px-2.5 py-1.5 font-mono text-xs text-slate-100 outline-none focus:border-cyan-500/60"
        />
        <input
          type="date"
          value={tradeDate}
          onChange={(e) => setTradeDate(e.target.value)}
          className="w-[7.5rem] rounded-lg border border-slate-700 bg-slate-950/70 px-2 py-1.5 text-xs text-slate-300 outline-none focus:border-cyan-500/60"
        />
      </div>

      <div className="flex items-center justify-between text-[11px]">
        <span className="text-slate-500">Provider</span>
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          className="rounded border border-slate-700 bg-slate-950/70 px-1.5 py-1 text-[11px] text-slate-200"
        >
          {PROVIDERS.map((p) => {
            const keyed = keyedProviders[p.value]
            return (
              <option key={p.value} value={p.value}>
                {p.label}
                {keyed === false ? ' (no key)' : ''}
              </option>
            )
          })}
        </select>
      </div>

      <button
        type="button"
        onClick={() => setAdvanced((v) => !v)}
        className="text-[11px] text-cyan-400/80 hover:text-cyan-300"
      >
        {advanced ? '▾ fewer options' : '▸ more options'}
      </button>

      {advanced && (
        <div className="space-y-2 rounded-lg border border-slate-800 bg-slate-950/40 p-2">
          <label className="flex items-center justify-between text-[11px]">
            <span className="text-slate-500">Deep model</span>
            <input
              value={deepModel}
              onChange={(e) => setDeepModel(e.target.value)}
              placeholder="default"
              className="ml-2 w-36 rounded border border-slate-700 bg-slate-950/70 px-1.5 py-1 font-mono text-[11px] text-slate-200"
            />
          </label>
          <label className="flex items-center justify-between text-[11px]">
            <span className="text-slate-500">Quick model</span>
            <input
              value={quickModel}
              onChange={(e) => setQuickModel(e.target.value)}
              placeholder="default"
              className="ml-2 w-36 rounded border border-slate-700 bg-slate-950/70 px-1.5 py-1 font-mono text-[11px] text-slate-200"
            />
          </label>
          <label className="flex items-center justify-between text-[11px]">
            <span className="text-slate-500">Reasoning effort</span>
            <select
              value={effort}
              onChange={(e) => setEffort(e.target.value)}
              className="rounded border border-slate-700 bg-slate-950/70 px-1.5 py-1 text-[11px]"
            >
              {EFFORTS.map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </label>
          {([
            ['Debate rounds', debateRounds, setDebateRounds],
            ['Risk rounds', riskRounds, setRiskRounds],
          ] as [string, number, (v: number) => void][]).map(([label, value, setter]) => (
            <label key={label} className="flex items-center justify-between text-[11px]">
              <span className="text-slate-500">{label}</span>
              <span className="flex items-center gap-1">
                {[1, 2, 3].map((n) => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setter(n)}
                    className={`h-6 w-6 rounded text-[11px] ${
                      value === n ? 'bg-cyan-500/30 text-cyan-200' : 'border border-slate-700 text-slate-400'
                    }`}
                  >
                    {n}
                  </button>
                ))}
              </span>
            </label>
          ))}
          <p className="text-[10px] leading-snug text-slate-600">
            One run ≈ dozens of LLM calls; higher round counts cost proportionally more.
          </p>
        </div>
      )}

      <button
        type="button"
        onClick={submit}
        disabled={running || !ticker.trim()}
        className={`flex w-full items-center justify-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold transition ${
          running
            ? 'cursor-not-allowed border-slate-700 text-slate-500'
            : 'border-emerald-500/60 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25'
        }`}
      >
        {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
        {running ? 'Analysis in progress…' : 'Convene TradingAgents'}
      </button>
    </div>
  )
}
