/**
 * Trading Room Settings — who the agents are, and what they are allowed to do.
 *
 * Two halves: the roster (rename, reseat, re-task, re-prompt each agent) and the
 * execution policy (whether the board may place real orders, on which venue,
 * with how much risk). Live trading stays behind two switches and the .env flag.
 */
import { useEffect, useMemo, useState } from 'react'
import Head from 'next/head'
import Link from 'next/link'
import { AlertTriangle, ArrowLeft, Cpu, Loader2, Save, ShieldCheck, Sparkles } from 'lucide-react'

import { apiClient } from '@/services/api'
import { useRoomSettings, type ExecutionPolicy, type RoomProfile } from '@/hooks/useRoomSettings'
import { OblivionCoveragePanel, PairApprovalPanel, TradingAccountsPanel, UniversePanel, WeeklyTargetPanel } from '@/components/TradingAccountsPanel'
import { useBtcCycleState } from '@/hooks/useBtcCycle'

// A connected AI provider (from the AI Market Analyst plugin) and its models.
interface AiProvider {
  id: number
  provider_key: string
  label: string
  enabled: boolean
  status: string
  default_model: string | null
  models: string[]
}
interface AiPreset {
  key: string
  label: string
  default_model: string
  models: string[]
}
// One provider's selectable models, for the per-seat Model dropdown.
interface ModelGroup {
  key: string
  label: string
  status: string
  models: string[]
}

// Best model per seat. Deep-reasoning seats — the ones judged on the quality of
// the read — get GLM-5.2; seats that run every tick and are judged on latency
// get Nemotron 3.5 Lightning. Ultra 550B is deliberately not here: it cannot
// answer inside a normal deadline, so it timed out on every call.
// Keep in step with ROLE_TASKS in backend/app/agents/specialists.py.
const ROLE_MODEL_PREFS: Record<string, { providerKey: string; model: string }[]> = {
  ceo:                [{ providerKey: 'nvidia', model: 'z-ai/glm-5.2' }],
  market_analyst:     [{ providerKey: 'nvidia', model: 'z-ai/glm-5.2' }],
  position_reviewer:  [{ providerKey: 'nvidia', model: 'z-ai/glm-5.2' }],
  strategy_optimizer: [{ providerKey: 'nvidia', model: 'z-ai/glm-5.2' }],
  sentiment_analyst:  [{ providerKey: 'nvidia', model: 'nvidia/nemotron-3.5-lightning-30b-a3b' }],
  signal_generator:   [{ providerKey: 'nvidia', model: 'nvidia/nemotron-3.5-lightning-30b-a3b' }],
  risk_manager:       [{ providerKey: 'cerebras', model: 'gpt-oss-120b' }],
  trade_executor:     [{ providerKey: 'cerebras', model: 'gpt-oss-120b' }],
}

// Must stay in step with FOCUS_INTERVAL_CHOICES in app/workers/room_worker.py —
// the API rejects anything else with a 400.
const FOCUS_INTERVALS = [
  { seconds: 300, label: '5 min' },
  { seconds: 900, label: '15 min' },
  { seconds: 3600, label: '1 hour' },
  { seconds: 7200, label: '2 hours' },
  { seconds: 14400, label: '4 hours' },
] as const

// Must stay in step with FOCUS_TIMEFRAME_CHOICES in app/workers/room_worker.py.
// This is the timeframe the agents analyse on AND the one the room's wall chart
// draws — one setting, so the board's argument and the picture behind it are
// never two different reads of the same pair.
const FOCUS_TIMEFRAMES = ['5m', '15m', '30m', '1h', '2h', '4h', '1d'] as const

function Toggle({
  label, hint, checked, onChange, tone = 'normal', disabled = false,
}: {
  label: string
  hint?: string
  checked: boolean
  onChange: (v: boolean) => void
  tone?: 'normal' | 'danger'
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`flex w-full items-start gap-3 rounded-lg border p-3 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${
        checked
          ? tone === 'danger'
            ? 'border-red-500/60 bg-red-500/10'
            : 'border-emerald-500/50 bg-emerald-500/10'
          : 'border-slate-700 bg-slate-900/50 hover:border-slate-500'
      }`}
    >
      <span
        className={`mt-0.5 h-5 w-9 shrink-0 rounded-full p-0.5 transition ${
          checked ? (tone === 'danger' ? 'bg-red-500' : 'bg-emerald-500') : 'bg-slate-600'
        }`}
      >
        <span className={`block h-4 w-4 rounded-full bg-white transition ${checked ? 'translate-x-4' : ''}`} />
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-medium text-slate-100">{label}</span>
        {hint && <span className="mt-0.5 block text-[11px] leading-snug text-slate-400">{hint}</span>}
      </span>
    </button>
  )
}

function NumberField({
  label, hint, value, onChange, min, max, step = 1, suffix,
}: {
  label: string
  hint?: string
  value: number
  onChange: (v: number) => void
  min: number
  max: number
  step?: number
  suffix?: string
}) {
  return (
    <label className="block">
      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
      <span className="mt-1 flex items-center gap-2">
        <input
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100 focus:border-cyan-500 focus:outline-none"
        />
        {suffix && <span className="text-xs text-slate-500">{suffix}</span>}
      </span>
      {hint && <span className="mt-1 block text-[11px] text-slate-500">{hint}</span>}
    </label>
  )
}

function AgentCard({
  profile, onChange, modelGroups, allModelIds,
}: {
  profile: RoomProfile
  onChange: (patch: Partial<RoomProfile>) => void
  modelGroups: ModelGroup[]
  allModelIds: Set<string>
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-4">
      <div className="flex items-center gap-3">
        <input
          type="color"
          value={profile.color}
          onChange={(e) => onChange({ color: e.target.value })}
          className="h-9 w-9 shrink-0 cursor-pointer rounded-full border-0 bg-transparent p-0"
          title="Seat colour"
        />
        <input
          value={profile.human_name}
          onChange={(e) => onChange({ human_name: e.target.value })}
          placeholder="Name"
          className="w-32 rounded-lg border border-slate-700 bg-slate-900/70 px-2.5 py-1.5 text-sm font-semibold text-slate-100 focus:border-cyan-500 focus:outline-none"
        />
        <input
          value={profile.title}
          onChange={(e) => onChange({ title: e.target.value })}
          placeholder="Job title"
          className="flex-1 rounded-lg border border-slate-700 bg-slate-900/70 px-2.5 py-1.5 text-sm text-slate-300 focus:border-cyan-500 focus:outline-none"
        />
        {/* Body build + hair for this seat in the 3D room. Rendering only —
            nothing in the trading logic reads it. */}
        <div className="flex shrink-0 overflow-hidden rounded-lg border border-slate-700" role="group" aria-label="Avatar gender">
          {(['male', 'female'] as const).map((g) => (
            <button
              key={g}
              type="button"
              aria-pressed={profile.gender === g}
              onClick={() => onChange({ gender: g })}
              title={`Show ${profile.human_name} as ${g} in the 3D room`}
              className={`px-2.5 py-1.5 text-[11px] font-medium capitalize transition ${
                profile.gender === g
                  ? 'bg-cyan-600 text-white'
                  : 'bg-slate-900/70 text-slate-400 hover:text-slate-200'
              }`}
            >
              {g}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => onChange({ is_active: !profile.is_active })}
          className={`shrink-0 rounded-full px-2.5 py-1 text-[10px] font-medium ${
            profile.is_active ? 'bg-emerald-500/20 text-emerald-300' : 'bg-slate-700/60 text-slate-400'
          }`}
        >
          {profile.is_active ? 'At the table' : 'Benched'}
        </button>
      </div>

      <div className="mt-3">
        <label className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
          Standing instructions
        </label>
        <textarea
          value={profile.tasks ?? ''}
          onChange={(e) => onChange({ tasks: e.target.value })}
          rows={2}
          placeholder={`What should ${profile.human_name} focus on? e.g. "Prioritise London-session setups on gold; ignore signals during high-impact news."`}
          className="mt-1 w-full resize-y rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-[12px] text-slate-200 placeholder:text-slate-600 focus:border-cyan-500 focus:outline-none"
        />
        <p className="mt-1 text-[10px] text-slate-500">
          Appended to this agent&apos;s prompt every run. Hard risk limits still win.
        </p>
      </div>

      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="mt-2 text-[11px] text-cyan-400 hover:text-cyan-300"
      >
        {open ? 'Hide' : 'Show'} advanced — role, model, pairs, full prompt
      </button>

      {open && (
        <div className="mt-3 space-y-3 border-t border-slate-800 pt-3">
          <div className="grid grid-cols-3 gap-2">
            <label className="block">
              <span className="text-[10px] uppercase text-slate-500">Role key</span>
              <input
                value={profile.role}
                onChange={(e) => onChange({ role: e.target.value })}
                className="mt-1 w-full rounded border border-slate-700 bg-slate-900/70 px-2 py-1.5 font-mono text-[11px] text-slate-300"
              />
            </label>
            <label className="block">
              <span className="text-[10px] uppercase text-slate-500">Model</span>
              <select
                value={profile.model}
                onChange={(e) => onChange({ model: e.target.value })}
                className="mt-1 w-full rounded border border-slate-700 bg-slate-900/70 px-2 py-1.5 font-mono text-[11px] text-slate-300 focus:border-cyan-500 focus:outline-none"
              >
                {/* Keep the current value selectable even if no connected
                    provider offers it (a stale seat, or providers not loaded). */}
                {profile.model && !allModelIds.has(profile.model) && (
                  <option value={profile.model}>{profile.model} (current)</option>
                )}
                {modelGroups.length === 0 && !profile.model && (
                  <option value="">No providers connected</option>
                )}
                {modelGroups.map((g) => (
                  <optgroup key={g.key} label={g.status === 'error' ? `${g.label} ⚠` : g.label}>
                    {g.models.map((m) => (
                      <option key={`${g.key}:${m}`} value={m}>{m}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="text-[10px] uppercase text-slate-500">Pairs (blank = all)</span>
              <input
                value={profile.pairs ?? ''}
                onChange={(e) => onChange({ pairs: e.target.value })}
                placeholder="XAUUSD,BTCUSDT"
                className="mt-1 w-full rounded border border-slate-700 bg-slate-900/70 px-2 py-1.5 font-mono text-[11px] text-slate-300"
              />
            </label>
          </div>
          <label className="block">
            <span className="text-[10px] uppercase text-slate-500">System prompt</span>
            <textarea
              value={profile.system_prompt}
              onChange={(e) => onChange({ system_prompt: e.target.value })}
              rows={8}
              className="mt-1 w-full resize-y rounded border border-slate-700 bg-slate-900/70 px-2 py-1.5 font-mono text-[11px] text-slate-300"
            />
          </label>
        </div>
      )}
    </div>
  )
}

/**
 * Bitcoin 1064-day cycle — the calendar's spine and the auto-risk gate.
 *
 * Anchors are the cycle bottoms; the pattern projects a top 1064 days after
 * each and a bottom 365 days after that. Auto risk shrinks new entries inside
 * the projected-bear (or late-bull caution) window — off by default, because
 * the room's defaults are always inert until asked for.
 */
function CycleSettingsCard({
  policy,
  setPolicy,
  policyError,
}: {
  policy: ExecutionPolicy
  setPolicy: (p: Partial<ExecutionPolicy>) => Promise<void>
  policyError: string | null
}) {
  const { state: cycle } = useBtcCycleState()
  const [anchorsText, setAnchorsText] = useState<string | null>(null)
  const anchors = anchorsText ?? (policy.cycle_anchors ?? []).join('\n')
  const phaseColor = cycle?.phase === 'bull' ? 'text-emerald-300' : 'text-red-300'

  const saveAnchors = async () => {
    const list = anchorsText
      ?.split(/[\n,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (list) await setPolicy({ cycle_anchors: list })
  }

  return (
    <section className="space-y-4 rounded-xl border border-slate-700/70 bg-slate-900/30 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">Bitcoin cycle</h2>
        {cycle?.ok && (
          <span className={`text-[11px] font-semibold uppercase ${phaseColor}`}>
            live: {cycle.phase} · day {cycle.day_of_cycle} ·{' '}
            {cycle.phase === 'bull' ? 'top' : 'bottom'} in {Math.max(0, cycle.phase === 'bull' ? cycle.days_to_top : cycle.days_to_bottom)}d
          </span>
        )}
        <Link href="/bitcoin-cycle" className="ml-auto text-[11px] text-cyan-400 hover:text-cyan-300">
          open the cycle page →
        </Link>
      </div>
      <p className="text-[11px] leading-snug text-slate-500">
        The calendar every seat reads. Each bottom starts a ≈1064-day bull phase into a
        projected top, then ≈365 days down to the next bottom — the pattern every completed
        BTC cycle has followed since launch.
      </p>

      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
            Cycle bottoms (one ISO date per line)
          </span>
          <textarea
            value={anchors}
            onChange={(e) => setAnchorsText(e.target.value)}
            onBlur={() => { void saveAnchors() }}
            rows={4}
            spellCheck={false}
            className="mt-1.5 w-full rounded-lg border border-slate-700 bg-slate-900/50 px-3 py-2 font-mono text-xs text-slate-200 focus:border-cyan-500 focus:outline-none"
            placeholder={'2015-01-14\n2018-12-15\n2022-11-21'}
          />
          <p className="mt-1 text-[11px] text-slate-500">
            Saved on blur. The newest bottom starts the live cycle; earlier ones become the
            pattern&apos;s validation history.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <NumberField label="Bull days" value={policy.cycle_bull_days ?? 1064} min={200} max={2000} step={1}
            hint="Bottom → projected top."
            onChange={(v) => { void setPolicy({ cycle_bull_days: v }) }} />
          <NumberField label="Bear days" value={policy.cycle_bear_days ?? 365} min={60} max={1200} step={1}
            hint="Projected top → next bottom."
            onChange={(v) => { void setPolicy({ cycle_bear_days: v }) }} />
          <NumberField label="History years" value={policy.cycle_history_years ?? 15} min={1} max={20} step={1}
            hint="How many years of monthly candles the cycle chart loads — as far back as the calendar read reaches."
            onChange={(v) => { void setPolicy({ cycle_history_years: v }) }} />
          <div className="col-span-2 space-y-3">
            <Toggle
              label="Auto risk reduction in the projected-bear window"
              hint="When the calendar is in the bear phase — or inside the caution window before the projected top — new entries on cycle-driven pairs are sized smaller. Advisory elsewhere; this is the only thing the cycle does to orders."
              checked={policy.cycle_auto_risk ?? false}
              onChange={(v) => { void setPolicy({ cycle_auto_risk: v }) }}
            />
            <NumberField label="Risk multiplier" suffix="×" value={policy.cycle_risk_multiplier ?? 0.5} min={0.1} max={1} step={0.1}
              hint="Multiplies Risk / trade inside the caution window. Never raises it."
              onChange={(v) => { void setPolicy({ cycle_risk_multiplier: v }) }} />
          </div>
        </div>
      </div>

      {policyError && <p className="text-[11px] text-red-400">{policyError}</p>}
    </section>
  )
}

export default function TradingRoomSettingsPage() {
  const { profiles, policy, loading, error, saveProfiles, savePolicy } = useRoomSettings()
  const [draft, setDraft] = useState<RoomProfile[]>([])
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [policyError, setPolicyError] = useState<string | null>(null)
  const [confirmLive, setConfirmLive] = useState(false)

  // Connected AI providers + presets → the Model dropdown and Recommended Setup.
  const [providers, setProviders] = useState<AiProvider[]>([])
  const [presets, setPresets] = useState<AiPreset[]>([])
  const [recoMsg, setRecoMsg] = useState<string | null>(null)

  useEffect(() => { setDraft(profiles) }, [profiles])

  useEffect(() => {
    Promise.all([
      apiClient.aiAnalyst.getProviders(),
      apiClient.aiAnalyst.getProviderPresets(),
    ])
      .then(([p, pr]) => {
        setProviders((p.data ?? []) as AiProvider[])
        setPresets((pr.data ?? []) as AiPreset[])
      })
      .catch(() => { /* plugin may be unavailable — dropdown falls back to free text */ })
  }, [])

  // Selectable models grouped by connected provider (enabled only). Falls back
  // to the preset's model list, then the provider's single default_model.
  const modelGroups = useMemo<ModelGroup[]>(() => {
    return providers
      .filter((p) => p.enabled)
      .map((p) => {
        const preset = presets.find((pr) => pr.key === p.provider_key)
        let models = p.models?.length ? p.models : (preset?.models ?? [])
        if (!models.length && p.default_model) models = [p.default_model]
        models = Array.from(new Set(models.filter(Boolean)))
        return { key: p.provider_key, label: p.label, status: p.status, models }
      })
      .filter((g) => g.models.length > 0)
  }, [providers, presets])

  const allModelIds = useMemo(
    () => new Set(modelGroups.flatMap((g) => g.models)),
    [modelGroups],
  )

  // Best available model for a seat: try its preferred provider/model, then that
  // provider's default, then any NVIDIA, then any connected provider's default.
  const pickBestModel = (role: string): string | null => {
    const enabled = providers.filter((p) => p.enabled)
    const pool = (enabled.filter((p) => p.status !== 'error').length
      ? enabled.filter((p) => p.status !== 'error')
      : enabled)
    const has = (p: AiProvider, m: string) =>
      p.default_model === m || (p.models ?? []).includes(m)

    for (const pref of ROLE_MODEL_PREFS[role] ?? []) {
      const exact = pool.find((p) => p.provider_key === pref.providerKey && has(p, pref.model))
      if (exact) return pref.model
      const sameProvider = pool.find((p) => p.provider_key === pref.providerKey && p.default_model)
      if (sameProvider?.default_model) return sameProvider.default_model
    }
    const nvidia = pool.find((p) => p.provider_key === 'nvidia' && p.default_model)
    if (nvidia?.default_model) return nvidia.default_model
    return pool.find((p) => p.default_model)?.default_model ?? null
  }

  const applyRecommended = async () => {
    if (modelGroups.length === 0) {
      setRecoMsg('No AI providers connected yet — add free keys on Telegram Signals → Connect AI first.')
      return
    }
    let assigned = 0
    setDraft((prev) =>
      prev.map((d) => {
        const best = pickBestModel(d.role)
        if (best && best !== d.model) assigned += 1
        return best ? { ...d, model: best } : d
      }),
    )
    // Make sure the room actually spends the connected providers, not the local key.
    try { await apiClient.aiAnalyst.updateRouterSettings({ agents_use_providers: true }) } catch { /* best-effort */ }
    setRecoMsg(`Best available models assigned to ${assigned} seat${assigned === 1 ? '' : 's'}. Review below, then Save roster.`)
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(profiles)

  const patch = (agentId: number, p: Partial<RoomProfile>) =>
    setDraft((prev) => prev.map((x) => (x.agent_id === agentId ? { ...x, ...p } : x)))

  const onSaveRoster = async () => {
    setSaving(true)
    try {
      await saveProfiles(draft.map((d) => ({
        agent_id: d.agent_id,
        human_name: d.human_name,
        title: d.title,
        color: d.color,
        seat: d.seat,
        gender: d.gender,
        tasks: d.tasks,
        role: d.role,
        model: d.model,
        pairs: d.pairs,
        system_prompt: d.system_prompt,
        is_active: d.is_active,
      })))
      setSavedAt(new Date().toLocaleTimeString())
    } finally {
      setSaving(false)
    }
  }

  const setPolicy = async (p: Partial<ExecutionPolicy>) => {
    setPolicyError(await savePolicy(p))
  }

  return (
    <>
      <Head><title>Trading Room Settings — TradeBot</title></Head>

      <div className="mx-auto max-w-6xl space-y-6 p-1">
        <div className="flex items-center gap-3">
          <Link href="/trading-room" className="rounded-lg border border-slate-700 p-2 text-slate-400 hover:border-slate-500">
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div>
            <h1 className="text-xl font-semibold text-slate-100">Trading Room Settings</h1>
            <p className="text-[12px] text-slate-400">Who sits at the table, and what they may do with your money.</p>
          </div>
        </div>

        {loading && (
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        )}
        {error && (
          <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
        )}

        {policy && (
          <section className="space-y-4 rounded-xl border border-slate-700/70 bg-slate-900/30 p-4">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-cyan-400" />
              <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">Execution</h2>
              {policy.execution_enabled && (
                <span className={`ml-auto rounded-full px-2.5 py-1 text-[10px] font-medium ${
                  policy.dry_run ? 'bg-amber-500/20 text-amber-300' : 'bg-red-500/20 text-red-300'
                }`}>
                  {policy.dry_run ? 'ARMED — DEMO ACCOUNTS ONLY' : 'ARMED — DEMO + LIVE ACCOUNTS'}
                </span>
              )}
            </div>

            {policy.execution_enabled && !policy.dry_run && (
              <div className="flex gap-2 rounded-lg border border-red-500/50 bg-red-500/10 p-3">
                <AlertTriangle className="h-4 w-4 shrink-0 text-red-400" />
                <p className="text-[12px] leading-snug text-red-200">
                  Agents are placing <strong>real orders on every switched-on live account</strong> without asking,
                  including while this page is closed. Demo accounts take every trade alongside them. Turn live
                  trading off to keep trading on the demo accounts only.
                </p>
              </div>
            )}
            {policyError && (
              <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-3 text-[12px] text-amber-200">
                {policyError}
              </div>
            )}

            <div className="grid gap-3 md:grid-cols-2">
              <Toggle
                label="Let agents execute trades"
                hint="Master switch. Off means the room only analyses and recommends."
                checked={policy.execution_enabled}
                onChange={(v) => setPolicy({ execution_enabled: v })}
              />
              <div>
                <Toggle
                  label="Live trading"
                  hint={
                    policy.global_auto_trading_enabled
                      ? 'Off: only demo accounts trade — they are always on. On: every live account switched on below trades too.'
                      : 'Locked off until ENABLE_AUTO_TRADING=true is set in your .env.'
                  }
                  tone="danger"
                  checked={!policy.dry_run}
                  onChange={(on) => (on ? setConfirmLive(true) : setPolicy({ dry_run: true }))}
                />
                {confirmLive && policy.dry_run && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-red-500/50 bg-red-500/10 p-2 text-[12px] text-red-200">
                    <span className="flex-1">Real money: every switched-on live account will take the room&apos;s trades.</span>
                    <button type="button" className="rounded bg-red-600 px-2 py-1 font-medium text-white"
                      onClick={() => { setConfirmLive(false); void setPolicy({ dry_run: false }) }}>
                      Turn live trading on
                    </button>
                    <button type="button" className="rounded border border-slate-600 px-2 py-1 text-slate-300"
                      onClick={() => setConfirmLive(false)}>
                      Cancel
                    </button>
                  </div>
                )}
              </div>
            </div>

            <div>
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">Venues</h3>
              <div className="grid gap-3 md:grid-cols-3">
                <Toggle label="Simulation" hint="Paper account — no money at risk."
                  checked={policy.allow_sim} onChange={(v) => setPolicy({ allow_sim: v })} />
                <Toggle label="Crypto (Bitget)" hint="Futures via the live trade engine." tone="danger"
                  checked={policy.allow_crypto} onChange={(v) => setPolicy({ allow_crypto: v })} />
                <Toggle label="MT5" hint="Market orders with SL/TP on the account below." tone="danger"
                  checked={policy.allow_mt5} onChange={(v) => setPolicy({ allow_mt5: v })} />
              </div>
              <p className="mt-2 text-[11px] text-slate-500">
                Routed by instrument, not by priority: FX, metals and indices go to MT5, crypto goes to
                the exchange, and the simulation account records every trade alongside whichever ran.
                A crypto pair falls back to MT5 if the exchange is off, and vice versa.
              </p>
            </div>

            {policy.allow_mt5 && (
              <div className="space-y-3">
                <label className="block">
                  <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Primary MT5 demo account</span>
                  <select
                    value={policy.mt5_demo_account_id ?? ''}
                    onChange={(e) => setPolicy({ mt5_demo_account_id: e.target.value ? Number(e.target.value) : null })}
                    className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100 focus:border-cyan-500 focus:outline-none"
                  >
                    <option value="">— none selected —</option>
                    {(policy.mt5_accounts ?? []).map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name} · {a.login} · {a.equity?.toFixed?.(2)} {a.currency}
                      </option>
                    ))}
                  </select>
                  <p className="mt-1 text-[10px] text-slate-500">
                    Every demo account trades every OBLIVION-confirmed signal in both modes. This one is also
                    where the desk&apos;s daily-loss limit is measured.
                  </p>
                </label>

              </div>
            )}

            <WeeklyTargetPanel
              target={policy.weekly_target_usd ?? 3000}
              pacing={policy.target_pacing ?? true}
              maxMult={policy.target_max_multiplier ?? 2}
              dayMin={policy.daily_target_min_pct ?? 10}
              dayMax={policy.daily_target_max_pct ?? 20}
              perPair={policy.target_per_pair ?? true}
              minRisk={policy.min_risk_pct ?? 1}
              maxRisk={policy.max_risk_pct ?? 5}
              newsPre={policy.news_pre_min ?? 30}
              newsCool={policy.news_cooloff_min ?? 120}
              onSave={(patch) => void setPolicy(patch as Partial<ExecutionPolicy>)}
            />

            <TradingAccountsPanel liveTrading={!policy.dry_run} />

            <UniversePanel
              enabled={policy.universe_enabled ?? false}
              symbols={policy.universe_symbols ?? ''}
              minWinRate={policy.pair_min_win_rate ?? 0.95}
              dailyFloor={policy.daily_min_win_rate ?? 0.9}
              recoveryBar={policy.daily_recovery_min_wr ?? 0.97}
              onSave={(patch) => void setPolicy(patch as Partial<ExecutionPolicy>)}
            />

            <PairApprovalPanel />

            <OblivionCoveragePanel timeframe="2h" />
            <OblivionCoveragePanel timeframe="15m" />

            <div>
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">Trade management — real time</h3>
              <div className="grid gap-3 md:grid-cols-3">
                <NumberField label="Check every" suffix="s" value={policy.manage_tick_s ?? 5} min={3} max={60}
                  hint="Break-even, trailing and margin checks on every open room trade."
                  onChange={(v) => setPolicy({ manage_tick_s: v })} />
                <NumberField label="Margin warning" suffix="%" value={policy.margin_warn_pct ?? 300} min={50} max={2000} step={10}
                  hint="Below this, the account takes no new entries."
                  onChange={(v) => setPolicy({ margin_warn_pct: v })} />
                <NumberField label="Margin critical" suffix="%" value={policy.margin_critical_pct ?? 150} min={50} max={1000} step={10}
                  hint="Below this, the worst-losing room trade is cut in half until margin recovers."
                  onChange={(v) => setPolicy({ margin_critical_pct: v })} />
                <NumberField label="Account drawdown cap" suffix="%" value={policy.max_account_drawdown_pct ?? 5} min={0.5} max={50} step={0.5}
                  hint="Room trades down more than this share of balance are all closed."
                  onChange={(v) => setPolicy({ max_account_drawdown_pct: v })} />
                <NumberField label="Sudden move" suffix="× ATR" value={policy.shift_atr_mult ?? 1.5} min={0.3} max={10} step={0.1}
                  hint="A run against the trade this big triggers an immediate re-check."
                  onChange={(v) => setPolicy({ shift_atr_mult: v })} />
                <NumberField label="Bank on a turn" suffix="%" value={Math.round((policy.shift_take_profit_fraction ?? 0.5) * 100)} min={10} max={100} step={5}
                  hint="Share of a winning trade closed when the market turns against it; the rest is locked at break-even."
                  onChange={(v) => setPolicy({ shift_take_profit_fraction: v / 100 })} />
              </div>
            </div>

            <div>
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">Copy-trading supervision</h3>
              <div className="space-y-3">
                <Toggle
                  label="Agents manage all copy accounts"
                  hint="The room reviews every copy profile each cycle. It can disable profiles breaching drawdown or win-rate limits, disable followers stuck in errors, and log every decision to its supervision log. Re-enabling is always manual."
                  checked={policy.manage_copy_profiles ?? false}
                  onChange={(v) => setPolicy({ manage_copy_profiles: v })}
                />
                {(policy.manage_copy_profiles ?? false) && (
                  <NumberField
                    label="Copy max drawdown" suffix="%" value={policy.copy_max_drawdown_pct ?? 20}
                    min={1} max={100} step={1}
                    hint="A copy profile whose paper drawdown exceeds this gets paused by the agents."
                    onChange={(v) => setPolicy({ copy_max_drawdown_pct: v })}
                  />
                )}
              </div>
            </div>

            <div>
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">Risk limits</h3>
              <div className="grid gap-3 md:grid-cols-3 lg:grid-cols-5">
                <NumberField label="Risk / trade" suffix="%" value={policy.risk_pct} min={0.1} max={10} step={0.1}
                  hint="Of equity, sized off the stop distance."
                  onChange={(v) => setPolicy({ risk_pct: v })} />
                <NumberField label="Max positions" value={policy.max_open_positions} min={1} max={50}
                  onChange={(v) => setPolicy({ max_open_positions: v })} />
                <NumberField label="Min consensus" suffix="%" value={Math.round(policy.min_consensus * 100)} min={0} max={100} step={5}
                  hint="Board agreement needed."
                  onChange={(v) => setPolicy({ min_consensus: v / 100 })} />
                <NumberField label="Min confidence" suffix="%" value={Math.round(policy.min_confidence * 100)} min={0} max={100} step={5}
                  onChange={(v) => setPolicy({ min_confidence: v / 100 })} />
                <NumberField label="Max trades / day" value={policy.max_trades_per_day} min={1} max={100}
                  hint={`${policy.trades_today ?? 0} used today.`}
                  onChange={(v) => setPolicy({ max_trades_per_day: v })} />
              </div>
            </div>

            <div>
              <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-slate-400">Cadence</h3>
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                    Re-analyse the focused pair every
                  </span>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {FOCUS_INTERVALS.map((opt) => (
                      <button
                        key={opt.seconds}
                        type="button"
                        onClick={() => setPolicy({ focus_interval_s: opt.seconds })}
                        className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition ${
                          policy.focus_interval_s === opt.seconds
                            ? 'border-cyan-500 bg-cyan-500/15 text-cyan-200'
                            : 'border-slate-700 bg-slate-900/50 text-slate-400 hover:border-slate-500'
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  <p className="mt-1.5 text-[11px] text-slate-500">
                    A pinned pair keeps its own cadence — it is not held back by the
                    rotation cooldown that spaces out unpinned pairs.
                  </p>
                </div>

                <div className="space-y-3">
                  <Toggle
                    label="Keep the board meeting 24/7"
                    hint="The room analyses whether or not this page is open, and re-arms itself after a restart. It still meets on the cadence above — that setting is the room's clock either way."
                    checked={policy.worker_enabled ?? true}
                    onChange={(v) => setPolicy({ worker_enabled: v })}
                  />

                  <div>
                    <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
                      Analyse on the timeframe
                    </span>
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                      {FOCUS_TIMEFRAMES.map((tf) => (
                        <button
                          key={tf}
                          type="button"
                          onClick={() => setPolicy({ focus_timeframe: tf })}
                          className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition ${
                            (policy.focus_timeframe ?? '1h') === tf
                              ? 'border-cyan-500 bg-cyan-500/15 text-cyan-200'
                              : 'border-slate-700 bg-slate-900/50 text-slate-400 hover:border-slate-500'
                          }`}
                        >
                          {tf}
                        </button>
                      ))}
                    </div>
                    <p className="mt-1.5 text-[11px] text-slate-500">
                      The agents read this timeframe&apos;s candles, and the room&apos;s
                      wall chart draws the same one.
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}

        {/* ── Bitcoin 1064-day cycle ── */}
        {policy && (
          <CycleSettingsCard policy={policy} setPolicy={setPolicy} policyError={policyError} />
        )}

        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">The roster</h2>
            {savedAt && !dirty && <span className="text-[11px] text-emerald-400">Saved {savedAt}</span>}
            <button
              type="button"
              onClick={applyRecommended}
              title="Assign the best connected model to every seat — deep reasoning for analysis, wafer-speed for execution"
              className="ml-auto flex items-center gap-2 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-sm font-medium text-emerald-300 transition hover:border-emerald-400 hover:bg-emerald-500/20"
            >
              <Sparkles className="h-4 w-4" />
              Recommended Setup — Best Models
            </button>
            <button
              type="button"
              onClick={onSaveRoster}
              disabled={!dirty || saving}
              className="flex items-center gap-2 rounded-lg bg-cyan-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-cyan-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {dirty ? 'Save roster' : 'No changes'}
            </button>
          </div>

          <div className="flex items-start gap-2 text-[11px] text-slate-500">
            <Cpu className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" />
            {modelGroups.length > 0 ? (
              <span>
                Each seat&apos;s Model picks from your connected providers ({modelGroups.map((g) => g.label).join(', ')}).
                {' '}
                <Link href="/telegram-signals" className="text-cyan-400 hover:text-cyan-300">Manage providers →</Link>
              </span>
            ) : (
              <span>
                No AI providers connected — seats fall back to the local key.{' '}
                <Link href="/telegram-signals" className="text-cyan-400 hover:text-cyan-300">Connect free AI providers →</Link>
              </span>
            )}
          </div>
          {recoMsg && (
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-[12px] text-emerald-200">
              {recoMsg}
            </div>
          )}

          {draft.map((p) => (
            <AgentCard
              key={p.agent_id}
              profile={p}
              onChange={(patchObj) => patch(p.agent_id, patchObj)}
              modelGroups={modelGroups}
              allModelIds={allModelIds}
            />
          ))}

          {!loading && !draft.length && (
            <p className="text-sm text-slate-500">
              No agents yet — seed the defaults from the AI Agents page.
            </p>
          )}
        </section>
      </div>
    </>
  )
}
