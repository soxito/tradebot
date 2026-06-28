/**
 * AgentProvidersPanel
 *
 * Surfaces the AI providers connected on /telegram-signals directly on the
 * /agents page: which providers power the agents, their monthly usage +
 * remaining, per-agent token usage, and the load-balancing / token-budget
 * controls (strategy, free-tier reserve, per-agent max tokens, headroom +
 * graphify toggles). Everything is best-effort and degrades gracefully if the
 * AI Market Analyst plugin is unavailable.
 */
import { useCallback, useEffect, useState } from 'react'
import { apiClient } from '@/services/api'
import { Cpu, Gauge, Save, Sparkles, Network } from 'lucide-react'

interface ProviderUsage {
  id: number
  label: string
  enabled: boolean
  status: string
  monthly_calls: number
  monthly_limit: number | null
  monthly_remaining: number | null
  month_tokens: number
  total_errors: number
  last_model_used: string | null
}

interface RouterSettings {
  strategy: string
  agents_use_providers: boolean
  agent_token_mode: string
  per_agent_max_tokens: number
  reserve_pct: number
  headroom_enabled: boolean
  graphify_enabled: boolean
}

interface AgentUsageRow {
  agent_role: string
  calls: number
  total_tokens: number
  prompt_tokens: number
  completion_tokens: number
}

export default function AgentProvidersPanel() {
  const [usage, setUsage] = useState<any>(null)
  const [agentUsage, setAgentUsage] = useState<{ agents: AgentUsageRow[]; month_total_tokens: number; month_total_calls: number } | null>(null)
  const [settings, setSettings] = useState<RouterSettings | null>(null)
  const [draft, setDraft] = useState<RouterSettings | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [u, au, rs] = await Promise.all([
        apiClient.aiAnalyst.getAiUsage(),
        apiClient.aiAnalyst.getAiUsageAgents(),
        apiClient.aiAnalyst.getRouterSettings(),
      ])
      setUsage(u.data)
      setAgentUsage(au.data)
      setSettings(rs.data)
      setDraft(rs.data)
    } catch {
      /* plugin may be unavailable */
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [load])

  const save = async () => {
    if (!draft) return
    setSaving(true)
    setSavedMsg(null)
    try {
      const res = await apiClient.aiAnalyst.updateRouterSettings({
        strategy: draft.strategy,
        agents_use_providers: draft.agents_use_providers,
        agent_token_mode: draft.agent_token_mode,
        per_agent_max_tokens: draft.per_agent_max_tokens,
        reserve_pct: draft.reserve_pct,
        headroom_enabled: draft.headroom_enabled,
        graphify_enabled: draft.graphify_enabled,
      })
      setSettings(res.data)
      setDraft(res.data)
      setSavedMsg('Saved ✓')
      setTimeout(() => setSavedMsg(null), 2500)
    } catch {
      setSavedMsg('Save failed')
    } finally {
      setSaving(false)
    }
  }

  const providers: ProviderUsage[] = usage?.providers || []
  const totals = usage?.totals
  const hasProviders = providers.length > 0
  const dirty = draft && settings && JSON.stringify(draft) !== JSON.stringify(settings)

  return (
    <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5 space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="font-semibold text-white flex items-center gap-2">
          <Cpu className="w-5 h-5 text-cyan-400" /> AI Providers &amp; Token Budget
        </h2>
        <a href="/telegram-signals" className="text-xs text-cyan-400 hover:text-cyan-300">
          Manage providers in Telegram Signals →
        </a>
      </div>

      {!hasProviders && (
        <div className="text-xs text-amber-300 bg-amber-900/20 border border-amber-500/30 rounded p-3">
          No AI providers connected yet. Agents are using the local OpenAI key (if set).
          Connect free-tier providers on the <a href="/telegram-signals" className="underline">Telegram Signals → Connect AI</a> tab
          so agents share them with automatic load-balancing and monthly-tier protection.
        </div>
      )}

      {/* Overall totals */}
      {totals && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="bg-gray-900/50 rounded-lg p-3">
            <div className="text-[11px] text-gray-400">Monthly calls</div>
            <div className="text-lg font-bold text-white">
              {(totals.monthly_calls ?? 0).toLocaleString()}
              {totals.monthly_limit != null && <span className="text-xs text-gray-500"> / {totals.monthly_limit.toLocaleString()}</span>}
            </div>
          </div>
          <div className="bg-gray-900/50 rounded-lg p-3">
            <div className="text-[11px] text-gray-400">Monthly remaining</div>
            <div className="text-lg font-bold text-emerald-400">
              {totals.monthly_remaining != null ? totals.monthly_remaining.toLocaleString() : '∞'}
            </div>
          </div>
          <div className="bg-gray-900/50 rounded-lg p-3">
            <div className="text-[11px] text-gray-400">Tokens this month</div>
            <div className="text-lg font-bold text-cyan-300">{(totals.month_tokens ?? 0).toLocaleString()}</div>
          </div>
          <div className="bg-gray-900/50 rounded-lg p-3">
            <div className="text-[11px] text-gray-400">Agent calls (month)</div>
            <div className="text-lg font-bold text-purple-300">{(agentUsage?.month_total_calls ?? 0).toLocaleString()}</div>
          </div>
        </div>
      )}

      {/* Provider list */}
      {hasProviders && (
        <div className="space-y-2">
          {providers.map((p) => {
            const used = p.monthly_calls || 0
            const limit = p.monthly_limit
            const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0
            const bar = pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-amber-500' : 'bg-emerald-500'
            return (
              <div key={p.id}>
                <div className="flex justify-between text-[11px] text-gray-400 mb-0.5">
                  <span className="flex items-center gap-1.5">
                    <span className={`w-1.5 h-1.5 rounded-full ${p.enabled ? (p.status === 'ok' ? 'bg-emerald-400' : 'bg-amber-400') : 'bg-gray-600'}`} />
                    {p.label}
                    {p.last_model_used && <span className="text-gray-600">· {p.last_model_used}</span>}
                    <span className="text-gray-600">· {(p.month_tokens ?? 0).toLocaleString()} tok</span>
                  </span>
                  <span>
                    {used.toLocaleString()}{limit != null ? ` / ${limit.toLocaleString()}` : ' calls'}
                    {p.monthly_remaining != null && <span className="text-emerald-400"> · {p.monthly_remaining.toLocaleString()} left</span>}
                  </span>
                </div>
                {limit != null && (
                  <div className="h-1.5 bg-gray-900 rounded-full overflow-hidden">
                    <div className={`h-full ${bar} transition-all`} style={{ width: `${pct}%` }} />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Per-agent token usage */}
      {agentUsage && agentUsage.agents.length > 0 && (
        <div>
          <h3 className="text-xs font-semibold text-gray-300 mb-2 flex items-center gap-1.5">
            <Gauge className="w-3.5 h-3.5 text-purple-400" /> Per-agent token usage (this month)
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 text-left border-b border-gray-700">
                  <th className="py-1.5 pr-3 font-medium">Agent role</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Calls</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Prompt</th>
                  <th className="py-1.5 pr-3 font-medium text-right">Completion</th>
                  <th className="py-1.5 font-medium text-right">Total tokens</th>
                </tr>
              </thead>
              <tbody>
                {agentUsage.agents.map((a) => (
                  <tr key={a.agent_role} className="border-b border-gray-800/50">
                    <td className="py-1.5 pr-3 text-gray-200">{a.agent_role}</td>
                    <td className="py-1.5 pr-3 text-right text-gray-400">{a.calls.toLocaleString()}</td>
                    <td className="py-1.5 pr-3 text-right text-gray-400">{a.prompt_tokens.toLocaleString()}</td>
                    <td className="py-1.5 pr-3 text-right text-gray-400">{a.completion_tokens.toLocaleString()}</td>
                    <td className="py-1.5 text-right font-semibold text-cyan-300">{a.total_tokens.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Token-budget controls */}
      {draft && (
        <div className="border-t border-gray-700 pt-4">
          <h3 className="text-xs font-semibold text-gray-300 mb-3 flex items-center gap-1.5">
            <Network className="w-3.5 h-3.5 text-cyan-400" /> Load balancing &amp; token budget
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            <label className="block">
              <span className="text-[11px] text-gray-400">Strategy</span>
              <select
                value={draft.strategy}
                onChange={(e) => setDraft({ ...draft, strategy: e.target.value })}
                className="w-full mt-1 rounded bg-gray-900 border border-gray-700 px-2 py-1.5 text-sm text-white"
              >
                <option value="round_robin">Round-robin (spread the load)</option>
                <option value="least_used">Least used (most headroom first)</option>
                <option value="priority">Priority (deterministic failover)</option>
              </select>
            </label>
            <label className="block">
              <span className="text-[11px] text-gray-400">When agents spend tokens</span>
              <select
                value={draft.agent_token_mode}
                onChange={(e) => setDraft({ ...draft, agent_token_mode: e.target.value })}
                className="w-full mt-1 rounded bg-gray-900 border border-gray-700 px-2 py-1.5 text-sm text-white"
              >
                <option value="telegram_only">Telegram signals only (save daily tier)</option>
                <option value="always">Always (background scans too)</option>
              </select>
            </label>
            <label className="block">
              <span className="text-[11px] text-gray-400">Per-agent max tokens</span>
              <input
                type="number" min={100} max={8000} step={100}
                value={draft.per_agent_max_tokens}
                onChange={(e) => setDraft({ ...draft, per_agent_max_tokens: parseInt(e.target.value) || 0 })}
                className="w-full mt-1 rounded bg-gray-900 border border-gray-700 px-2 py-1.5 text-sm text-white"
              />
            </label>
            <label className="block">
              <span className="text-[11px] text-gray-400">Free-tier reserve buffer (%)</span>
              <input
                type="number" min={0} max={90} step={5}
                value={Math.round((draft.reserve_pct || 0) * 100)}
                onChange={(e) => setDraft({ ...draft, reserve_pct: (parseInt(e.target.value) || 0) / 100 })}
                className="w-full mt-1 rounded bg-gray-900 border border-gray-700 px-2 py-1.5 text-sm text-white"
              />
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-4 mt-3">
            <Toggle
              label="Agents use connected providers"
              checked={draft.agents_use_providers}
              onChange={(v) => setDraft({ ...draft, agents_use_providers: v })}
            />
            <Toggle
              label="Headroom compression"
              icon={<Sparkles className="w-3 h-3 text-emerald-400" />}
              checked={draft.headroom_enabled}
              onChange={(v) => setDraft({ ...draft, headroom_enabled: v })}
            />
            <Toggle
              label="Graphify map"
              icon={<Network className="w-3 h-3 text-cyan-400" />}
              checked={draft.graphify_enabled}
              onChange={(v) => setDraft({ ...draft, graphify_enabled: v })}
            />
          </div>
          <div className="flex items-center gap-3 mt-4">
            <button
              onClick={save}
              disabled={!dirty || saving}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 disabled:cursor-not-allowed rounded text-sm font-medium transition"
            >
              <Save className="w-3.5 h-3.5" /> {saving ? 'Saving…' : 'Save settings'}
            </button>
            {savedMsg && <span className="text-xs text-emerald-400">{savedMsg}</span>}
            <span className="text-[11px] text-gray-500">
              Reserve keeps a buffer so free monthly tiers are never fully spent.
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

function Toggle({ label, checked, onChange, icon }: { label: string; checked: boolean; onChange: (v: boolean) => void; icon?: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 text-xs text-gray-300"
    >
      <span className={`relative inline-flex h-4 w-7 items-center rounded-full transition ${checked ? 'bg-cyan-600' : 'bg-gray-600'}`}>
        <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition ${checked ? 'translate-x-3.5' : 'translate-x-0.5'}`} />
      </span>
      <span className="flex items-center gap-1">{icon}{label}</span>
    </button>
  )
}
