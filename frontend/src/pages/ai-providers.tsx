/**
 * AI Providers — which brains the desk is actually connected to.
 *
 * The router already load-balances and rate-limits across every enabled key;
 * this page is the missing window onto it. Several rows may share a provider
 * (that is how multiple keys are held), and the strategy picker decides how
 * calls are spread across them.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import Head from 'next/head'
import {
  AlertTriangle, CheckCircle2, Loader2, Plus, RefreshCw, Trash2, XCircle, Zap,
} from 'lucide-react'

import { apiClient } from '@/services/api'
import {
  useAiProviders, type AiProvider, type ProviderPreset, type RouterSettings,
} from '@/hooks/useAiProviders'

const STRATEGIES: { key: RouterSettings['strategy']; label: string; hint: string }[] = [
  { key: 'priority', label: 'Priority', hint: 'Always try the lowest priority number first. Deterministic failover — but it hammers one key until it rate-limits.' },
  { key: 'round_robin', label: 'Round robin', hint: 'Rotate the starting point so calls spread evenly across every key.' },
  { key: 'least_used', label: 'Least used', hint: 'Send to whichever key has the most monthly headroom left. Best when free tiers have different caps.' },
]

function statusTone(p: AiProvider) {
  if (!p.enabled) return { cls: 'bg-slate-700/60 text-slate-400', label: 'disabled', Icon: XCircle }
  if (p.status === 'ok') return { cls: 'bg-emerald-500/20 text-emerald-300', label: 'connected', Icon: CheckCircle2 }
  if (p.status === 'error') return { cls: 'bg-red-500/20 text-red-300', label: 'error', Icon: AlertTriangle }
  return { cls: 'bg-slate-700/60 text-slate-300', label: 'untested', Icon: XCircle }
}

/** A usage bar against a free-tier cap. Null cap means unlimited. */
function UsageBar({ used, limit, label }: { used: number; limit: number | null; label: string }) {
  if (!limit) {
    return <div className="text-[11px] text-slate-500">{label}: {used} (no cap)</div>
  }
  const pct = Math.min(100, Math.round((used / limit) * 100))
  const tone = pct >= 90 ? 'bg-red-500' : pct >= 70 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div>
      <div className="flex justify-between text-[11px] text-slate-400">
        <span>{label}</span>
        <span>{used} / {limit}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-800">
        <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function ProviderRow({
  provider, siblings, onToggle, onDelete, onTest, onModel, testing,
}: {
  provider: AiProvider
  /** How many keys are configured for this same provider. */
  siblings: number
  onToggle: (v: boolean) => void
  onDelete: () => void
  onTest: () => void
  onModel: (model: string) => void
  testing: boolean
}) {
  const tone = statusTone(provider)
  const [confirming, setConfirming] = useState(false)

  return (
    <div className="rounded-xl border border-slate-700/70 bg-slate-900/50 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${tone.cls}`}>
          <tone.Icon className="h-3 w-3" />
          {tone.label}
        </span>

        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-slate-100">{provider.label}</span>
            {siblings > 1 && (
              <span
                className="rounded bg-cyan-500/15 px-1.5 py-0.5 text-[10px] text-cyan-300"
                title="More than one key is configured for this provider — the router spreads calls across them."
              >
                {siblings} keys
              </span>
            )}
            {provider.free_tier && (
              <span className="rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-300">free tier</span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-slate-500">
            <span>{provider.provider_key} · priority {provider.priority} ·</span>
            {provider.api_key_preview ? (
              <code
                className="rounded bg-slate-800/80 px-1.5 py-0.5 font-mono text-[11px] text-slate-300"
                title="First five and last four characters — check this before pasting a key you may already hold."
              >
                {provider.api_key_preview}
              </code>
            ) : (
              <span className="text-amber-400">no key</span>
            )}
            {provider.total_calls > 0 && (
              <span>· {provider.total_calls} calls, {provider.total_errors} errors</span>
            )}
          </div>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={onTest}
            disabled={testing}
            className="flex items-center gap-1.5 rounded-lg border border-slate-700 px-2.5 py-1.5 text-[11px] text-slate-300 transition hover:border-slate-500 disabled:opacity-50"
          >
            {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
            Test
          </button>
          <button
            type="button"
            onClick={() => onToggle(!provider.enabled)}
            className={`rounded-lg border px-2.5 py-1.5 text-[11px] transition ${
              provider.enabled
                ? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-300'
                : 'border-slate-700 text-slate-400 hover:border-slate-500'
            }`}
          >
            {provider.enabled ? 'Enabled' : 'Disabled'}
          </button>
          {confirming ? (
            <span className="flex items-center gap-1">
              <button
                type="button"
                onClick={onDelete}
                className="rounded-lg bg-red-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-red-500"
              >
                Delete key
              </button>
              <button
                type="button"
                onClick={() => setConfirming(false)}
                className="rounded-lg border border-slate-700 px-2 py-1.5 text-[11px] text-slate-400"
              >
                Cancel
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => setConfirming(true)}
              className="rounded-lg border border-slate-700 p-1.5 text-slate-400 transition hover:border-red-500/60 hover:text-red-300"
              title="Remove this key"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <label className="block md:col-span-1">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Model</span>
          <select
            value={provider.default_model ?? ''}
            onChange={(e) => onModel(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-2.5 py-1.5 text-sm text-slate-100 focus:border-cyan-500 focus:outline-none"
          >
            {!provider.models?.length && <option value="">{provider.default_model ?? '—'}</option>}
            {provider.models?.map((m) => (
              <option key={m} value={m}>
                {provider.model_info?.[m]?.label ?? m}
              </option>
            ))}
          </select>
          {provider.default_model && provider.model_info?.[provider.default_model]?.best_for && (
            <span className="mt-1 block text-[11px] leading-snug text-slate-500">
              {provider.model_info[provider.default_model].best_for}
            </span>
          )}
        </label>

        <div className="space-y-2 md:col-span-2">
          <UsageBar used={provider.daily_calls} limit={provider.daily_limit} label="Today" />
          <UsageBar used={provider.monthly_calls} limit={provider.monthly_limit} label="This month" />
        </div>
      </div>

      {provider.status === 'error' && provider.last_error && (
        <p className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-[11px] leading-snug text-red-300">
          {provider.last_error.slice(0, 300)}
        </p>
      )}
    </div>
  )
}

/** Mask a key the same way the API does, so typed input can be compared to it. */
function maskKey(key: string): string | null {
  const k = key.trim()
  if (!k) return null
  if (k.length < 16) return '•'.repeat(8)
  return `${k.slice(0, 5)}…${k.slice(-4)}`
}

function AddProviderForm({
  presets, existing, onAdd, onClose,
}: {
  presets: ProviderPreset[]
  /** Already-connected keys, so a duplicate is caught before it is submitted. */
  existing: AiProvider[]
  onAdd: (body: any) => Promise<void>
  onClose: () => void
}) {
  const [presetKey, setPresetKey] = useState(presets[0]?.key ?? '')
  const [apiKey, setApiKey] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const preset = presets.find((p) => p.key === presetKey)
  const alreadyHere = existing.filter((p) => p.provider_key === presetKey)

  // Live check: the mask of what has been typed against the masks we hold. The
  // server is still the authority (it compares full keys) — this just stops the
  // mistake before the round trip.
  const typedMask = maskKey(apiKey)
  const clash = typedMask
    ? existing.find((p) => p.api_key_preview && p.api_key_preview === typedMask)
    : undefined

  const submit = async () => {
    if (!apiKey.trim()) { setErr('Paste the API key first.'); return }
    setBusy(true); setErr(null)
    try {
      await onAdd({
        provider_key: presetKey,
        api_key: apiKey.trim(),
        label: label.trim() || undefined,
      })
      onClose()
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? 'Could not add that key.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl border border-cyan-500/40 bg-cyan-500/5 p-4">
      <h3 className="text-sm font-semibold text-slate-100">Connect a provider</h3>
      <p className="mt-1 text-[11px] text-slate-400">
        Adding a second key for a provider you already have is supported and encouraged —
        the router spreads calls across every key to stay under each free tier.
      </p>

      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <label className="block">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Provider</span>
          <select
            value={presetKey}
            onChange={(e) => setPresetKey(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-2.5 py-1.5 text-sm text-slate-100 focus:border-cyan-500 focus:outline-none"
          >
            {presets.map((p) => (
              <option key={p.key} value={p.key}>{p.label}</option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">Label (optional)</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={preset ? `${preset.label} #2` : 'Second key'}
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-2.5 py-1.5 text-sm text-slate-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>

        <label className="block">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">API key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="Paste the key"
            autoComplete="off"
            className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-2.5 py-1.5 font-mono text-sm text-slate-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>
      </div>

      {alreadyHere.length > 0 && (
        <div className="mt-3 rounded-lg border border-slate-700 bg-slate-900/60 p-2.5">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">
            Keys already connected for {preset?.label ?? presetKey}
          </span>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {alreadyHere.map((p) => (
              <code
                key={p.id}
                className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[11px] text-slate-300"
              >
                {p.api_key_preview ?? 'no key'}
              </code>
            ))}
          </div>
          <p className="mt-1.5 text-[11px] text-slate-500">
            Compare the first five and last four characters before pasting.
          </p>
        </div>
      )}

      {clash && (
        <p className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300">
          That looks like the key already connected as “{clash.label}”. Adding it
          again would draw one quota down from two rows and rate-limit twice as fast.
        </p>
      )}

      {preset?.notes && <p className="mt-2 text-[11px] text-slate-500">{preset.notes}</p>}
      {preset?.signup_url && (
        <a
          href={preset.signup_url}
          target="_blank"
          rel="noreferrer"
          className="mt-1 inline-block text-[11px] text-cyan-400 hover:underline"
        >
          Get a key from {preset.label} →
        </a>
      )}
      {err && <p className="mt-2 text-[11px] text-red-400">{err}</p>}

      <div className="mt-3 flex gap-2">
        <button
          type="button"
          onClick={submit}
          disabled={busy}
          className="flex items-center gap-2 rounded-lg bg-cyan-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-cyan-500 disabled:bg-slate-700"
        >
          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
          Connect
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-400 hover:border-slate-500"
        >
          Cancel
        </button>
      </div>
    </div>
  )
}

/** Human labels for the router's task keys. */
const TASK_LABELS: Record<string, { title: string; hint: string }> = {
  vision_analysis: {
    title: 'Chart / image reads',
    hint: 'Screenshots sent to the Telegram bot, Paul chat or the extension.',
  },
  fast_agentic: {
    title: 'Fast agentic turns',
    hint: 'Bot replies, position checks and quick lookups — latency matters more than depth.',
  },
  deep_reasoning: {
    title: 'Deep reasoning',
    hint: 'Strategy synthesis, forecast narration, full market analysis.',
  },
  jarvis_chat: {
    title: 'JARVIS chat',
    hint: 'JARVIS answers once its own model chain is exhausted. Optional — unset, it uses any available provider.',
  },
  paul_chat: {
    title: 'Agent Paul chat',
    hint: 'Paul’s conversational turns. Optional — unset, it uses any available provider.',
  },
  telegram_chat: {
    title: 'Telegram bot chat',
    hint: 'Free-text replies in the Telegram bot. Optional — unset, it uses any available provider.',
  },
}

/**
 * Which provider profile serves which task — and the guarantee that a
 * dedicated one serves nothing else.
 *
 * The point is quota isolation: a slow vision read on a shared key eats the
 * same rate limit the chat path needs, and the first symptom is the *other*
 * feature timing out. Pinning one profile per task makes that impossible, so
 * the panel shows the binding rather than leaving it to trust.
 */
function TaskDedicationPanel({
  providers,
  onChanged,
}: {
  providers: AiProvider[]
  onChanged: () => void
}) {
  const [tasks, setTasks] = useState<any[]>([])
  const [meta, setMeta] = useState<{ keys_needed: number; signup_urls: any[] }>({ keys_needed: 0, signup_urls: [] })
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await apiClient.aiAnalyst.getTaskAssignments()
      setTasks(res.data?.tasks ?? [])
      setMeta({
        keys_needed: res.data?.keys_needed ?? 0,
        signup_urls: res.data?.signup_urls ?? [],
      })
      setErr(null)
    } catch {
      setTasks([])
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const assign = async (task: string, providerId: number | null) => {
    setBusy(task)
    setErr(null)
    try {
      await apiClient.aiAnalyst.assignTaskProfile(task, providerId)
      await load()
      onChanged()
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? 'Could not change the assignment')
    } finally {
      setBusy(null)
    }
  }

  if (!tasks.length) return null

  // A profile already dedicated elsewhere is shown but not selectable, so the
  // "one profile, one job" rule is visible in the picker instead of only
  // arriving as an error after you try.
  const takenBy = new Map<number, string>()
  for (const t of tasks) if (t.provider_id) takenBy.set(t.provider_id, t.task)

  return (
    <section className="rounded-xl border border-slate-700/70 bg-slate-900/40 p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
        Dedicated profiles — one key per job
      </h2>
      <p className="mt-1 text-xs text-slate-400">
        A profile pinned to a task is used by that task <strong className="text-slate-300">only</strong>,
        and is held out of the shared pool entirely — so its rate limit is never
        spent by anything else. Leave a task on the shared pool to keep the old
        load-balanced behaviour.
      </p>

      {err && (
        <div className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
          {err}
        </div>
      )}

      {meta.keys_needed > 0 && (
        <div className="mt-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5">
          <div className="text-xs font-semibold text-amber-300">
            {meta.keys_needed} more API {meta.keys_needed === 1 ? 'key' : 'keys'} needed
          </div>
          <p className="mt-1 text-[11px] leading-snug text-amber-200/80">
            The brain roles run at the same time and argue with each other, so each
            needs its own key. Sharing one serialises the cycle behind a single rate
            limit and has the critic reviewing the consolidator on the model that
            wrote it. All of these have free tiers:
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            {meta.signup_urls.map((s: any) => (
              <a
                key={s.url}
                href={s.url}
                target="_blank"
                rel="noopener noreferrer"
                title={s.note}
                className="rounded bg-amber-500/20 px-2 py-1 text-[11px] font-medium text-amber-200 transition hover:bg-amber-500/30"
              >
                {s.label} →
              </a>
            ))}
          </div>
        </div>
      )}

      {(['work', 'surface', 'brain'] as const).map((group) => {
        const inGroup = tasks.filter((t) => (t.group ?? 'work') === group)
        if (!inGroup.length) return null
        const heading = group === 'work' ? 'Work types'
          : group === 'surface' ? 'Chat surfaces — optional'
          : 'JARVIS brain network — a key each'
        return (
          <div key={group} className="mt-4">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              {heading}
            </div>
            <div className="mt-2 grid gap-3 md:grid-cols-3">
              {inGroup.map((t) => {
          const label = TASK_LABELS[t.task] ?? { title: t.label ?? t.task, hint: '' }
          return (
            <div
              key={t.task}
              className={`rounded-lg border p-3 ${
                t.needs_key
                  ? 'border-amber-500/50 bg-amber-500/5'
                  : 'border-slate-700/60 bg-slate-950/40'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-slate-200">{label.title}</span>
                <span className={`text-[10px] font-semibold ${
                  t.dedicated ? 'text-emerald-400' : t.needs_key ? 'text-amber-400' : 'text-slate-500'
                }`}>
                  {t.dedicated ? '🔒 exclusive' : t.needs_key ? '⚠ needs a key' : 'shared pool'}
                </span>
              </div>
              <p className="mt-1 text-[11px] leading-snug text-slate-500">{label.hint}</p>

              <select
                value={t.provider_id ?? ''}
                disabled={busy === t.task}
                onChange={(e) => void assign(t.task, e.target.value ? Number(e.target.value) : null)}
                className="mt-2 w-full rounded border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-200 disabled:opacity-50"
              >
                <option value="">Shared pool (no dedicated profile)</option>
                {providers.map((p) => {
                  const heldBy = takenBy.get(p.id)
                  const elsewhere = heldBy && heldBy !== t.task
                  return (
                    <option key={p.id} value={p.id} disabled={!!elsewhere}>
                      {p.label}
                      {elsewhere ? ` — dedicated to ${TASK_LABELS[heldBy!]?.title ?? heldBy}` : ''}
                    </option>
                  )
                })}
              </select>

              <div className="mt-2 text-[10px] leading-snug text-slate-500">
                {t.models?.length ? (
                  <>
                    <span className="text-slate-400">Model chain:</span>{' '}
                    <span className="font-mono">{t.models.join(' → ')}</span>
                  </>
                ) : t.dedicated
                  // Surface and brain slots pin no models on purpose — they run
                  // on whatever the profile they were given offers.
                  ? 'Runs on the dedicated profile’s own models'
                  : t.required
                    ? 'Falling back to the shared pool until a key is set'
                    : 'Not set — uses any available provider'}
              </div>
              {t.dedicated && t.provider_status === 'error' && (
                <div className="mt-1 text-[10px] text-amber-400">
                  This profile last reported an error — test it below.
                </div>
              )}
            </div>
          )
              })}
            </div>
          </div>
        )
      })}
    </section>
  )
}

export default function AiProvidersPage() {
  const {
    providers, presets, settings, loading, error, testing,
    reload, addProvider, updateProvider, deleteProvider, testProvider, testAll, saveSettings,
  } = useAiProviders()
  const [adding, setAdding] = useState(false)

  // How many keys exist per provider — drives the "N keys" badge.
  const keyCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const p of providers) counts[p.provider_key] = (counts[p.provider_key] ?? 0) + 1
    return counts
  }, [providers])

  const connected = providers.filter((p) => p.enabled && p.status === 'ok').length
  const erroring = providers.filter((p) => p.enabled && p.status === 'error').length

  return (
    <>
      <Head><title>AI Providers — TradeBot</title></Head>

      <div className="mx-auto max-w-6xl space-y-5 p-4">
        <header className="flex flex-wrap items-center gap-3">
          <div>
            <h1 className="text-xl font-bold text-slate-100">AI Providers</h1>
            <p className="text-sm text-slate-400">
              The brains your agents think with. Calls are load-balanced across every
              enabled key, with cooldowns when one rate-limits.
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={() => void testAll()}
              disabled={testing === 'all'}
              className="flex items-center gap-2 rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 transition hover:border-slate-500 disabled:opacity-50"
            >
              {testing === 'all' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
              Test all
            </button>
            <button
              type="button"
              onClick={() => void reload()}
              className="rounded-lg border border-slate-700 p-2 text-slate-400 transition hover:border-slate-500"
              title="Refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={() => setAdding((v) => !v)}
              className="flex items-center gap-2 rounded-lg bg-cyan-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-cyan-500"
            >
              <Plus className="h-4 w-4" />
              Add key
            </button>
          </div>
        </header>

        {error && (
          <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {!loading && !error && (
          <div className="flex flex-wrap gap-2 text-[11px]">
            <span className="rounded-full bg-emerald-500/15 px-2.5 py-1 text-emerald-300">
              {connected} connected
            </span>
            {erroring > 0 && (
              <span className="rounded-full bg-red-500/15 px-2.5 py-1 text-red-300">
                {erroring} erroring
              </span>
            )}
            <span className="rounded-full bg-slate-800/70 px-2.5 py-1 text-slate-300">
              {providers.length} keys total
            </span>
          </div>
        )}

        {adding && (
          <AddProviderForm
            presets={presets}
            existing={providers}
            onAdd={async (body) => { await addProvider(body) }}
            onClose={() => setAdding(false)}
          />
        )}

        {/* ── Dedicated profiles per task ── */}
        {!loading && !error && (
          <TaskDedicationPanel providers={providers} onChanged={() => void reload()} />
        )}

        {/* ── Routing policy ── */}
        {settings && (
          <section className="rounded-xl border border-slate-700/70 bg-slate-900/40 p-4">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">Routing</h2>

            <div className="mt-3 grid gap-2 md:grid-cols-3">
              {STRATEGIES.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => void saveSettings({ strategy: s.key })}
                  className={`rounded-lg border p-3 text-left transition ${
                    settings.strategy === s.key
                      ? 'border-cyan-500 bg-cyan-500/10'
                      : 'border-slate-700 bg-slate-900/50 hover:border-slate-500'
                  }`}
                >
                  <span className="block text-sm font-medium text-slate-100">{s.label}</span>
                  <span className="mt-1 block text-[11px] leading-snug text-slate-400">{s.hint}</span>
                </button>
              ))}
            </div>

            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <button
                type="button"
                onClick={() => void saveSettings({ agents_use_providers: !settings.agents_use_providers })}
                className={`rounded-lg border p-3 text-left transition ${
                  settings.agents_use_providers
                    ? 'border-emerald-500/50 bg-emerald-500/10'
                    : 'border-slate-700 bg-slate-900/50'
                }`}
              >
                <span className="block text-sm font-medium text-slate-100">
                  Agents use these providers — {settings.agents_use_providers ? 'ON' : 'OFF'}
                </span>
                <span className="mt-1 block text-[11px] text-slate-400">
                  Off means the trading room falls back to the single local OpenAI key.
                </span>
              </button>

              <button
                type="button"
                onClick={() => void saveSettings({
                  agent_token_mode: settings.agent_token_mode === 'always' ? 'telegram_only' : 'always',
                })}
                className={`rounded-lg border p-3 text-left transition ${
                  settings.agent_token_mode === 'always'
                    ? 'border-emerald-500/50 bg-emerald-500/10'
                    : 'border-amber-500/50 bg-amber-500/10'
                }`}
              >
                <span className="block text-sm font-medium text-slate-100">
                  Background analysis — {settings.agent_token_mode === 'always' ? 'ALWAYS' : 'TELEGRAM ONLY'}
                </span>
                <span className="mt-1 block text-[11px] text-slate-400">
                  {settings.agent_token_mode === 'always'
                    ? 'The room spends tokens on its own scanning, so the board never idles.'
                    : 'The continuous pair-scanner is skipped to preserve free tiers — agents only run on Telegram signals and manual requests.'}
                </span>
              </button>
            </div>
          </section>
        )}

        {loading && (
          <p className="flex items-center gap-2 text-sm text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading providers…
          </p>
        )}

        <section className="space-y-3">
          {providers
            .slice()
            .sort((a, b) => a.priority - b.priority || a.id - b.id)
            .map((p) => (
              <ProviderRow
                key={p.id}
                provider={p}
                siblings={keyCounts[p.provider_key] ?? 1}
                testing={testing === p.id}
                onTest={() => void testProvider(p.id)}
                onToggle={(v) => void updateProvider(p.id, { enabled: v })}
                onModel={(m) => void updateProvider(p.id, { default_model: m })}
                onDelete={() => void deleteProvider(p.id)}
              />
            ))}

          {!loading && !providers.length && !error && (
            <p className="rounded-xl border border-slate-700/70 bg-slate-900/40 p-6 text-center text-sm text-slate-500">
              No providers connected yet. Add a key to give the agents something to think with.
            </p>
          )}
        </section>
      </div>
    </>
  )
}
