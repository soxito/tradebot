import Head from 'next/head'
import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { apiClient } from '@/services/api'
import {
  Sparkles, RefreshCw, Search, Brain, Zap, Users, Shield, Cpu, Database,
  Globe, Plug, FileText, Activity, CheckCircle2, XCircle, Clock, AlertTriangle,
  Settings, Trash2, Save, ExternalLink, Radio, MessageSquare, Layers, Atom,
} from 'lucide-react'

interface Overview {
  enabled: boolean
  gateway: { reachable: boolean; url: string; error?: string }
  config: { retention_days: number; auto_ingest: boolean; cron_enabled: boolean; gateway_url: string; state_path: string; skills_path: string; soul_path: string }
  stats: { episodes_total: number; episodes_24h: number; last_ingest_ts: number | null; fts_kb: number; db_path: string; skills_count: number }
  features: { id: string; label: string; desc: string; enabled: boolean; status: string; detail: string }[]
  agents: { role: string; human_name: string; title: string; color: string; seat: number; agent_id: number | null; agent_name: string; model: string; is_active: boolean; decisions_total: number; last_decision_at?: string; connected: boolean; source: string }[]
  skills: { name: string; path: string; meta: any }[]
  execution_allowed: boolean
  profile: Record<string, any>
  profile_fragment: string
  soul: { path: string; exists: boolean; preview: string; length?: number }
  repo: { cloned: boolean; commit?: string | null; commit_full?: string | null; branch?: string | null; commit_date?: string | null; pkg_version?: string | null; path: string; remote: string; last_pull?: string | null; dirty?: boolean }
}

function timeAgo(ts: number | null): string {
  if (!ts) return '—'
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function StatCard({ label, value, sub, icon }: any) {
  return (
    <div className="bg-gray-800/40 border border-gray-700/60 rounded-xl p-4">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-gray-400">
        {icon} {label}
      </div>
      <div className="text-xl font-bold text-white mt-1">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1 truncate">{sub}</div>}
    </div>
  )
}

export default function HermesPage() {
  const [data, setData] = useState<Overview | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [q, setQ] = useState('BTC momentum')
  const [sym, setSym] = useState('BTC/USDT')
  const [hits, setHits] = useState<any[]>([])
  const [searching, setSearching] = useState(false)
  const [searchSource, setSearchSource] = useState<string>('')
  const [profileForm, setProfileForm] = useState({ risk_pct: '', focus_symbol: '', focus_timeframe: '', preferred_pairs: '', notes: '' })
  const [saving, setSaving] = useState(false)
  const [pruning, setPruning] = useState(false)
  const [pulling, setPulling] = useState(false)
  // Best-trader skills (A+A+B): tabs, search, modal
  const [skillTab, setSkillTab] = useState<'all' | 'crypto' | 'forex'>('all')
  const [skillSearch, setSkillSearch] = useState('')
  const [skillDetail, setSkillDetail] = useState<any | null>(null)
  const [skillLoading, setSkillLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await apiClient.hermes.overview()
      const ov: Overview = r.data
      setData(ov)
      setProfileForm({
        risk_pct: ov.profile?.risk_pct != null ? String(ov.profile.risk_pct) : '',
        focus_symbol: ov.profile?.focus_symbol || '',
        focus_timeframe: ov.profile?.focus_timeframe || '',
        preferred_pairs: Array.isArray(ov.profile?.preferred_pairs) ? ov.profile.preferred_pairs.join(', ') : '',
        notes: ov.profile?.notes || '',
      })
      setErr(null)
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e.message || 'Failed to load Hermes overview')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const runSearch = async () => {
    if (!q.trim()) return
    setSearching(true)
    try {
      const r = await apiClient.hermes.search(q.trim(), sym.trim() || undefined, 8)
      setHits(r.data?.hits || [])
      setSearchSource(r.data?.source || '')
    } catch (e: any) {
      setHits([])
      setErr(e?.response?.data?.detail || 'Search failed')
    } finally { setSearching(false) }
  }

  const saveProfile = async () => {
    setSaving(true)
    try {
      const payload: any = {}
      if (profileForm.risk_pct !== '') payload.risk_pct = Number(profileForm.risk_pct)
      if (profileForm.focus_symbol) payload.focus_symbol = profileForm.focus_symbol.toUpperCase()
      if (profileForm.focus_timeframe) payload.focus_timeframe = profileForm.focus_timeframe
      if (profileForm.preferred_pairs) payload.preferred_pairs = profileForm.preferred_pairs.split(',').map(s => s.trim().toUpperCase()).filter(Boolean)
      if (profileForm.notes) payload.notes = profileForm.notes
      await apiClient.hermes.updateProfile(payload)
      await load()
    } catch (e: any) { setErr(e?.response?.data?.detail || 'Save failed') }
    finally { setSaving(false) }
  }

  const prune = async () => {
    setPruning(true)
    try { await apiClient.hermes.prune(); await load() } catch (e: any) { setErr(e?.response?.data?.detail || 'Prune failed') }
    finally { setPruning(false) }
  }

  const pullRepo = async () => {
    setPulling(true)
    try {
      const r = await apiClient.hermes.repoPull()
      const info = r.data?.updated ? `updated ${r.data.before} → ${r.data.after}` : (r.data?.ok ? 'already up-to-date' : r.data?.error || 'pull failed')
      setErr(null)
      // small toast via err banner reuse as success
      // reload overview to show new commit
      await load()
    } catch (e: any) { setErr(e?.response?.data?.detail || 'Pull failed') }
    finally { setPulling(false) }
  }

  const openSkill = async (name: string) => {
    setSkillLoading(true)
    setSkillDetail({ name, loading: true })
    try {
      const r = await apiClient.hermes.skill(name)
      setSkillDetail(r.data)
    } catch (e: any) {
      setSkillDetail({ error: e?.response?.data?.detail || 'Failed to load skill' })
    } finally { setSkillLoading(false) }
  }

  const connectedCount = data?.agents.filter(a => a.connected).length || 0
  const activeCount = data?.agents.filter(a => a.is_active).length || 0

  return (
    <>
      <Head><title>Hermes | TradeBot</title></Head>
      <div className="max-w-6xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-600 to-cyan-500 flex items-center justify-center">
              <Sparkles className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-white flex items-center gap-2">Hermes <span className="text-xs font-normal px-2 py-0.5 rounded-full bg-violet-500/20 text-violet-300 border border-violet-500/30">NousResearch/hermes-agent</span></h1>
              <p className="text-sm text-gray-400">Self-aware upgrade — episodic + skill + user-model · sidecar :8011 · SOUL: JARVIS/Paul/SOX merged</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className={`px-3 py-1.5 rounded-lg text-xs font-semibold border ${data?.enabled ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : 'bg-red-500/15 text-red-300 border-red-500/30'}`}>
              {data?.enabled ? '● Hermes enabled' : '● Hermes disabled'}
            </span>
            <button onClick={load} className="p-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300">
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </div>

        {err && (
          <div className="flex items-center gap-2 px-4 py-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-300 text-sm">
            <XCircle className="w-4 h-4" /> {err}
          </div>
        )}

        {loading && !data ? (
          <div className="flex items-center gap-2 text-gray-400 text-sm"><RefreshCw className="w-4 h-4 animate-spin" /> Loading Hermes…</div>
        ) : data && (
          <>
            {/* Repo version banner — integrated version that's running */}
            <div className="rounded-xl border bg-gray-800/40 border-gray-700/60 p-4 flex flex-wrap items-center gap-4">
              <div className="flex items-center gap-2">
                <div className={`w-2 h-2 rounded-full ${data.repo.cloned ? 'bg-emerald-400 animate-pulse' : 'bg-red-400'}`} />
                <span className="text-sm font-semibold text-white">hermes-agent repo</span>
                {data.repo.pkg_version && <span className="text-xs px-2 py-0.5 rounded bg-violet-500/20 text-violet-300 border border-violet-500/30">v{data.repo.pkg_version}</span>}
                {data.repo.commit && <span className="text-xs font-mono px-2 py-0.5 rounded bg-gray-900 border border-gray-700 text-gray-300">{data.repo.commit}</span>}
                {data.repo.branch && <span className="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-300">{data.repo.branch}</span>}
                {data.repo.dirty && <span className="text-xs px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">dirty</span>}
              </div>
              <div className="flex-1 min-w-0 text-xs text-gray-500 font-mono truncate">{data.repo.path} · {data.repo.remote}</div>
              <div className="flex items-center gap-2 text-xs text-gray-400">
                {data.repo.commit_date && <span>{new Date(data.repo.commit_date).toLocaleString()}</span>}
                {data.repo.last_pull && <span className="px-2 py-0.5 rounded bg-gray-900 border border-gray-700">last pull {timeAgo(data.repo.last_pull ? new Date(data.repo.last_pull).getTime()/1000 : null)}</span>}
                <button onClick={pullRepo} disabled={pulling} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white text-xs font-medium">
                  {pulling ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} Pull updates
                </button>
                <a href="https://github.com/NousResearch/hermes-agent" target="_blank" rel="noreferrer" className="flex items-center gap-1 px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 hover:text-white">
                  <ExternalLink className="w-3 h-3" /> GitHub
                </a>
              </div>
              {!data.repo.cloned && <span className="w-full text-xs text-amber-300">Repo not cloned yet — run <code className="px-1 py-0.5 bg-black/30 rounded">python3 start.py</code> to auto-clone to <code className="px-1 py-0.5 bg-black/30 rounded">integrations/hermes-agent</code></span>}
            </div>

            {/* Stat strip */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatCard icon={<Database className="w-3.5 h-3.5" />} label="Episodes" value={data.stats.episodes_total} sub={`${data.stats.episodes_24h} in 24h · ${data.stats.fts_kb} KB · ${data.stats.db_path}`} />
              <StatCard icon={<Zap className="w-3.5 h-3.5" />} label="Skills" value={data.stats.skills_count} sub={data.execution_allowed ? 'execution gate: open' : 'gate: RoomSettings.execution_enabled=false'} />
              <StatCard icon={<Users className="w-3.5 h-3.5" />} label="Agents connected" value={`${connectedCount}/${data.agents.length}`} sub={`${activeCount} active`} />
              <StatCard icon={<Globe className="w-3.5 h-3.5" />} label="Gateway" value={data.gateway.reachable ? 'reachable' : 'offline'} sub={data.gateway.url} />
              <StatCard icon={<Clock className="w-3.5 h-3.5" />} label="Last ingest" value={timeAgo(data.stats.last_ingest_ts)} sub={`retention ${data.config.retention_days}d · recall-only`} />
            </div>

            {/* Connected banner */}
            <div className={`rounded-xl border p-3 flex items-center gap-2 text-sm ${data.enabled ? 'bg-violet-500/10 border-violet-500/30 text-violet-200' : 'bg-gray-800/40 border-gray-700 text-gray-400'}`}>
              <Radio className="w-4 h-4" />
              {data.enabled
                ? <>Hermes sidecar is <strong>enabled</strong> — every Trading Room session + JARVIS turn auto-ingests into FTS5 when <code className="px-1 py-0.5 bg-black/30 rounded">HERMES_AUTO_INGEST=true</code>. Disable via <code className="px-1 py-0.5 bg-black/30 rounded">.env → HERMES_ENABLED=false</code>.</>
                : <>Hermes is <strong>disabled</strong> — no ingest, no recall. Enable with <code className="px-1 py-0.5 bg-black/30 rounded">HERMES_ENABLED=true</code> in <code className="px-1 py-0.5 bg-black/30 rounded">.env</code> and <code className="px-1 py-0.5 bg-black/30 rounded">docker compose --profile hermes up -d</code>.</>}
            </div>

            {/* Features in use */}
            <div>
              <h2 className="text-sm font-semibold text-white flex items-center gap-2 mb-3"><Layers className="w-4 h-4 text-violet-400" /> Hermes Features in Use</h2>
              <div className="grid md:grid-cols-2 gap-3">
                {data.features.map(f => (
                  <div key={f.id} className={`rounded-xl border p-4 ${f.enabled ? 'bg-gray-800/40 border-gray-700/60' : 'bg-gray-900/40 border-gray-800 opacity-70'}`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold text-white">{f.label}</span>
                      <span className={`text-[11px] px-2 py-1 rounded-full font-semibold border ${f.enabled ? (f.status === 'active' || f.status === 'connected' ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : f.status === 'ready' ? 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30' : 'bg-gray-700 text-gray-300 border-gray-600') : 'bg-gray-800 text-gray-500 border-gray-700'}`}>
                        {f.enabled ? f.status : 'disabled'}
                      </span>
                    </div>
                    <p className="text-xs text-gray-400 mt-1">{f.desc}</p>
                    <p className="text-xs text-gray-500 mt-1 font-mono truncate">{f.detail}</p>
                  </div>
                ))}
              </div>
            </div>

            {/* Agents connected */}
            <div>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-white flex items-center gap-2"><Users className="w-4 h-4 text-cyan-400" /> Agents Connected to Hermes <span className="text-xs font-normal text-gray-500">({connectedCount} connected · {activeCount} active)</span></h2>
                <Link href="/trading-room" className="text-xs text-cyan-400 hover:text-cyan-300 flex items-center gap-1">Trading Room <ExternalLink className="w-3 h-3" /></Link>
              </div>
              <div className="grid md:grid-cols-3 gap-3">
                {data.agents.map(a => (
                  <div key={a.role} className={`rounded-xl border p-4 flex gap-3 ${a.connected ? 'bg-gray-800/40 border-gray-700/60' : 'bg-gray-900/30 border-gray-800'}`}>
                    <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0 border" style={{ background: `${a.color}18`, borderColor: `${a.color}40`, color: a.color }}>
                      <Brain className="w-5 h-5" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-semibold text-white">{a.human_name}</span>
                        <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-300">{a.title}</span>
                        <span className={`w-2 h-2 rounded-full ${a.connected ? 'bg-emerald-400 animate-pulse' : 'bg-gray-600'}`} title={a.connected ? 'connected to Hermes' : 'not connected'} />
                      </div>
                      <div className="text-xs text-gray-500 truncate">{a.agent_name} · <span className="font-mono">{a.model}</span> · seat {a.seat} · {a.role}</div>
                      <div className="flex gap-2 text-[11px] mt-1">
                        <span className={a.is_active ? 'text-emerald-400' : 'text-gray-500'}>{a.is_active ? 'active' : 'inactive'}</span>
                        <span className="text-gray-600">·</span>
                        <span className="text-gray-400">{a.decisions_total} decisions</span>
                        {a.last_decision_at && <span className="text-gray-500 truncate">· last {new Date(a.last_decision_at).toLocaleDateString()}</span>}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              <p className="text-xs text-gray-500 mt-2">SOUL merged per 9.3: JARVIS (chair), Paul (chat), SOX (command room) are one <code className="px-1 py-0.5 bg-gray-800 rounded">SOUL.md</code> with voice variants — not separate agents.</p>
            </div>

            {/* Search test + skills + profile */}
            <div className="grid lg:grid-cols-3 gap-4">
              {/* Search */}
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Search className="w-4 h-4 text-cyan-400" /> Test Hermes Recall (FTS5)</h3>
                <p className="text-xs text-gray-500 mt-1">Search is recall-only and symbol-scoped. Try &quot;BTC momentum&quot; or a pair you trade.</p>
                <div className="flex gap-2 mt-3">
                  <input value={q} onChange={e => setQ(e.target.value)} placeholder="query (min 2 chars)" className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white" />
                  <input value={sym} onChange={e => setSym(e.target.value)} placeholder="BTC/USDT" className="w-28 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white" />
                </div>
                <button onClick={runSearch} disabled={searching || !q.trim()} className="mt-2 w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white text-sm font-medium">
                  {searching ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />} Search {searchSource && <span className="text-xs opacity-80">({searchSource})</span>}
                </button>
                <div className="mt-3 space-y-2 max-h-64 overflow-auto">
                  {hits.length === 0 && <div className="text-xs text-gray-600">{searching ? 'Searching…' : 'No hits yet — ingest a session first or enable Hermes.'}</div>}
                  {hits.map((h: any, i: number) => (
                    <div key={i} className="rounded-lg bg-gray-900/60 border border-gray-700/40 p-2">
                      <div className="text-[11px] text-gray-500">{h.symbol} · {h.kind} · {h.ts ? new Date(Number(h.ts) * 1000).toLocaleString() : ''}</div>
                      <div className="text-xs text-gray-200 mt-1 line-clamp-3">{h.content}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Skills — best-trader (A+A) + session harvest, linked to ALL agents + JARVIS, evolving (B) */}
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4 flex flex-col">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2">
                  <Zap className="w-4 h-4 text-amber-400" /> Best-Trader Skills
                  <span className="text-[11px] font-normal px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30">{data.skills.filter((s:any)=>s.is_best_trader).length} stock · {data.skills.filter((s:any)=>!s.is_best_trader).length} session</span>
                </h3>
                <p className="text-xs text-gray-500 mt-1">78 stock (14 crypto + 64 forex, 1 per pair) linked to ALL 7 seats + JARVIS chair. Evolving Learned block after 12+ resolved. Execution gated by RoomSettings.</p>
                {/* Tabs + search */}
                <div className="flex items-center gap-2 mt-3 flex-wrap">
                  {(['all','crypto','forex'] as const).map(tab => {
                    const n = tab==='all' ? data.skills.length : tab==='crypto' ? data.skills.filter((s:any)=>s.asset_class==='crypto').length : data.skills.filter((s:any)=> (s.asset_class||'').startsWith('forex')).length
                    const active = skillTab===tab
                    return (
                      <button key={tab} onClick={()=>setSkillTab(tab)} className={`px-3 py-1 rounded-full text-xs font-medium border ${active?'bg-violet-600 text-white border-violet-500':'bg-gray-800 text-gray-400 border-gray-700 hover:text-white'}`}>
                        {tab==='all'?'All':tab==='crypto'?'Crypto':'Forex'} <span className={`ml-1 px-1.5 py-0.5 rounded text-[10px] ${active?'bg-white/20':'bg-gray-900'}`}>{n}</span>
                      </button>
                    )
                  })}
                  <span className="ml-auto text-[11px] px-2 py-1 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30 flex items-center gap-1"><Sparkles className="w-3 h-3"/> JARVIS chair + 7 seats</span>
                </div>
                <input value={skillSearch} onChange={e=>setSkillSearch(e.target.value)} placeholder="Filter by symbol — e.g. EURUSD, BTC" className="mt-2 w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-white placeholder:text-gray-500" />
                {(() => {
                  let list:any[] = data.skills
                  if (skillTab==='crypto') list = list.filter((s:any)=>s.asset_class==='crypto')
                  else if (skillTab==='forex') list = list.filter((s:any)=>(s.asset_class||'').startsWith('forex'))
                  if (skillSearch.trim()) {
                    const q = skillSearch.trim().toUpperCase().replace(/[^A-Z0-9]/g,'')
                    list = list.filter((s:any)=> (s.symbol||s.meta?.symbol||s.name).toUpperCase().replace(/[^A-Z0-9]/g,'').includes(q))
                  }
                  if (list.length===0) return <div className="text-xs text-gray-600 mt-3">{skillSearch?'No match for "'+skillSearch+'" — try EURUSD, BTC, GBP':'No skills yet — run bootstrap or a Trading Room session.'}</div>
                  return (
                    <div className="grid gap-2 mt-3 max-h-72 overflow-auto pr-1">
                      {list.slice(0,60).map((s:any) => {
                        const isBT = !!s.is_best_trader
                        const symbol = s.symbol || s.meta?.symbol || s.name
                        const ac = s.asset_class || s.meta?.asset_class || ''
                        const group = s.group || s.meta?.group || ''
                        const evolved = !!s.evolved_at
                        const win = s.win_rate != null ? `${(s.win_rate*100).toFixed(0)}%` : null
                        return (
                          <button key={s.name} onClick={()=>openSkill(s.name)} className="text-left rounded-lg bg-gray-900/60 border border-gray-700/40 p-2.5 hover:border-violet-500/40 hover:bg-gray-900 transition group">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-xs font-bold text-white font-mono">{symbol}</span>
                              <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium border ${ac==='crypto'?'bg-orange-500/15 text-orange-300 border-orange-500/30':ac.startsWith('forex')?'bg-cyan-500/15 text-cyan-300 border-cyan-500/30':'bg-gray-800 text-gray-500 border-gray-700'}`}>{ac || 'skill'}</span>
                              {isBT && <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">stock</span>}
                              {!isBT && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-500 border border-gray-700">session {s.meta?.action||''}</span>}
                              {evolved && <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/30" title={`Evolved ${s.evolved_at}`}>Learned {win || '●'}</span>}
                            </div>
                            <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
                              <span className="text-[10px] text-gray-500 hidden sm:inline">{group}</span>
                              <span className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30"><Sparkles className="w-3 h-3"/>JARVIS</span>
                              <span className="flex -space-x-1">
                                {(s.linked_agents||['market_analyst','sentiment_analyst','signal_generator','risk_manager','trade_executor','position_reviewer','strategy_optimizer']).slice(0,7).map((role:string,i:number)=>{
                                  const colors:Record<string,string> = {market_analyst:'#3b82f6', sentiment_analyst:'#a855f7', signal_generator:'#22c55e', risk_manager:'#ef4444', trade_executor:'#f97316', position_reviewer:'#eab308', strategy_optimizer:'#14b8a6'}
                                  const c = colors[role]||'#94a3b8'
                                  return <span key={role} title={role} className="w-5 h-5 rounded-full border-2 border-gray-900 flex items-center justify-center text-[7px] font-bold text-white" style={{background:c}}>{role[0].toUpperCase()}</span>
                                })}
                              </span>
                              {s.decisions_reviewed ? <span className="text-[10px] text-gray-600">{s.decisions_reviewed} reviewed</span> : null}
                              <ExternalLink className="w-3 h-3 text-gray-600 group-hover:text-violet-400 ml-auto"/>
                            </div>
                            {s.content_preview && <div className="text-[11px] text-gray-600 mt-1 line-clamp-2">{s.content_preview.slice(0,120)}</div>}
                          </button>
                        )
                      })}
                      {list.length>60 && <div className="text-[11px] text-gray-500 text-center py-1">+{list.length-60} more — refine filter</div>}
                    </div>
                  )
                })()}
                <div className="flex gap-2 mt-3">
                  <Link href="/agents" className="inline-flex items-center gap-1 text-xs text-violet-400 hover:text-violet-300"><Cpu className="w-3 h-3" /> Manage agents <ExternalLink className="w-3 h-3" /></Link>
                  <span className="text-[11px] text-gray-600 ml-auto">Click a skill → modal with playbook + <code className="px-1 py-0.5 bg-gray-800 rounded">/skill SYMBOL</code> in chat</span>
                </div>
              </div>

              {/* Profile */}
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Settings className="w-4 h-4 text-emerald-400" /> Trader Profile</h3>
                <p className="text-xs text-gray-500 mt-1">Injected into specialist prompts. {data.profile_fragment ? <span className="text-emerald-400 font-mono">{data.profile_fragment}</span> : 'Empty — fill below.'}</p>
                <div className="space-y-2 mt-3">
                  <div className="grid grid-cols-2 gap-2">
                    <div><label className="text-xs text-gray-400">Risk % per trade</label><input value={profileForm.risk_pct} onChange={e => setProfileForm(p => ({ ...p, risk_pct: e.target.value }))} placeholder="1.0" className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" /></div>
                    <div><label className="text-xs text-gray-400">Focus symbol</label><input value={profileForm.focus_symbol} onChange={e => setProfileForm(p => ({ ...p, focus_symbol: e.target.value }))} placeholder="BTC/USDT" className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" /></div>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div><label className="text-xs text-gray-400">Timeframe</label><input value={profileForm.focus_timeframe} onChange={e => setProfileForm(p => ({ ...p, focus_timeframe: e.target.value }))} placeholder="1h" className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" /></div>
                    <div><label className="text-xs text-gray-400">Preferred pairs (CSV)</label><input value={profileForm.preferred_pairs} onChange={e => setProfileForm(p => ({ ...p, preferred_pairs: e.target.value }))} placeholder="BTC/USDT, ETH/USDT" className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" /></div>
                  </div>
                  <div><label className="text-xs text-gray-400">Notes</label><textarea value={profileForm.notes} onChange={e => setProfileForm(p => ({ ...p, notes: e.target.value }))} rows={2} placeholder="Trader style, constraints…" className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-sm text-white" /></div>
                  <button onClick={saveProfile} disabled={saving} className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-sm font-medium">
                    {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} Save profile
                  </button>
                </div>
              </div>
            </div>

            {/* Config + SOUL */}
            <div className="grid lg:grid-cols-2 gap-4">
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Cpu className="w-4 h-4 text-gray-400" /> Config + Repo</h3>
                <div className="mt-2 space-y-1 text-xs font-mono">
                  <div className="flex justify-between"><span className="text-gray-500">HERMES_ENABLED</span><span className={data.enabled ? 'text-emerald-400' : 'text-red-400'}>{String(data.enabled)}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">HERMES_GATEWAY_URL</span><span className="text-gray-300 truncate ml-2">{data.config.gateway_url}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">HERMES_STATE_PATH</span><span className="text-gray-300 truncate ml-2">{data.config.state_path}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">HERMES_SKILLS_PATH</span><span className="text-gray-300 truncate ml-2">{data.config.skills_path}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">SOUL_PATH</span><span className="text-gray-300">{data.config.soul_path}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Repo commit</span><span className="text-gray-300 font-mono">{data.repo.commit || '—'} {data.repo.commit_date ? `· ${new Date(data.repo.commit_date).toLocaleDateString()}` : ''}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Execution gate</span><span className={data.execution_allowed ? 'text-emerald-400' : 'text-amber-400'}>{data.execution_allowed ? 'open' : 'closed (RoomSettings.execution_enabled=false)'}</span></div>
                </div>
                <div className="flex gap-2 mt-3">
                  <button onClick={prune} disabled={pruning} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-xs text-white disabled:opacity-50">
                    {pruning ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />} Prune 90d now
                  </button>
                  <Link href="/trading-room-settings" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 border border-gray-700 text-xs text-gray-300 hover:text-white">
                    <Settings className="w-3.5 h-3.5" /> Room settings
                  </Link>
                </div>
                <p className="text-[11px] text-gray-600 mt-2">Scoring stays on Postgres AgentDecision — FTS5 is recall-only. Vault remains async exporter.</p>
              </div>
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><FileText className="w-4 h-4 text-violet-400" /> SOUL.md <span className="text-xs font-normal text-gray-500">({data.soul.exists ? `${data.soul.length} chars` : 'missing'})</span></h3>
                <pre className="mt-2 bg-gray-900/60 border border-gray-700/40 rounded-lg p-3 text-xs text-gray-300 whitespace-pre-wrap max-h-64 overflow-auto">{data.soul.preview || 'SOUL.md not found at ' + data.soul.path}</pre>
                <div className="flex gap-2 mt-2">
                  <span className="text-[11px] px-2 py-1 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30">jarvis variant</span>
                  <span className="text-[11px] px-2 py-1 rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">paul variant</span>
                  <span className="text-[11px] px-2 py-1 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">sox variant</span>
                </div>
                <p className="text-[11px] text-gray-600 mt-1">One soul, three surfaces — avatarStyle/voiceGender picks the variant (Layout.tsx).</p>
              </div>
            </div>
          </>
        )}
      </div>
      {/* Skill detail modal — A+A playbook + B evolution view */}
      {skillDetail && (
        <div className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-start justify-center p-4 overflow-auto" onClick={()=>setSkillDetail(null)}>
          <div className="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-3xl mt-8 shadow-2xl overflow-hidden" onClick={e=>e.stopPropagation()}>
            <div className="flex items-center justify-between p-4 border-b border-gray-700 bg-gray-800/50">
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-violet-600 to-cyan-500 flex items-center justify-center">
                  <Zap className="w-5 h-5 text-white" />
                </div>
                <div>
                  <div className="text-sm font-bold text-white flex items-center gap-2">
                    {skillDetail.symbol || skillDetail.slug || skillDetail.name || 'Skill'}
                    {skillDetail.asset_class && <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-700 text-gray-300">{skillDetail.asset_class}</span>}
                    {skillDetail.is_best_trader && <span className="text-[11px] px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">stock</span>}
                  </div>
                  <div className="text-xs text-gray-500">{skillDetail.group || ''} · linked to ALL 7 agents + JARVIS chair</div>
                </div>
              </div>
              <button onClick={()=>setSkillDetail(null)} className="p-2 hover:bg-gray-800 rounded-lg text-gray-400">
                <XCircle className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 max-h-[70vh] overflow-auto">
              {skillDetail.loading || skillLoading ? (
                <div className="flex items-center gap-2 text-sm text-gray-400"><RefreshCw className="w-4 h-4 animate-spin"/> Loading playbook…</div>
              ) : skillDetail.error ? (
                <div className="text-sm text-red-400">{skillDetail.error}</div>
              ) : (
                <>
                  <div className="flex flex-wrap gap-2 mb-3">
                    {(skillDetail.linked_agents||[]).map((r:string)=>(<span key={r} className="text-[11px] px-2 py-1 rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">{r}</span>))}
                    <span className="text-[11px] px-2 py-1 rounded bg-violet-500/15 text-violet-300 border border-violet-500/30 flex items-center gap-1"><Sparkles className="w-3 h-3"/>JARVIS ceo</span>
                    {skillDetail.meta?.evolved_at && <span className="text-[11px] px-2 py-1 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">Learned evolved {new Date(skillDetail.meta.evolved_at*1000).toLocaleDateString()} · {skillDetail.meta.win_rate!=null ? `${(skillDetail.meta.win_rate*100).toFixed(0)}%` : ''}</span>}
                  </div>
                  <pre className="bg-gray-950 border border-gray-800 rounded-lg p-4 text-xs text-gray-200 whitespace-pre-wrap leading-relaxed max-h-[55vh] overflow-auto">{skillDetail.md || 'No playbook content.'}</pre>
                  <div className="mt-3 text-xs text-gray-500 font-mono">Path: {skillDetail.path || skillDetail.meta?.path || ''}</div>
                  <div className="mt-3 flex gap-2">
                    <button onClick={async()=>{
                      const sym = skillDetail.symbol || skillDetail.slug?.split('-')[0] || ''
                      if(!sym) return
                      try{
                        const r = await apiClient.hermes.evolveSkill(sym, true)
                        setSkillDetail((prev:any)=>({...prev, _evolveRes: r.data}))
                        // reload overview to refresh win_rate badge
                        try{ const ov = await apiClient.hermes.overview(); /* no setData here, parent will reload on close */ }catch{}
                      }catch(e:any){ setSkillDetail((prev:any)=>({...prev, _evolveErr: e?.response?.data?.detail || 'evolve failed'}))}
                    }} className="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium">Evolve Learned (force)</button>
                    <button onClick={()=>{ if(skillDetail.symbol) { navigator.clipboard?.writeText(`/skill ${skillDetail.symbol}`); } }} className="px-3 py-1.5 rounded-lg bg-gray-800 border border-gray-700 text-gray-300 text-xs">Copy /skill {skillDetail.symbol}</button>
                  </div>
                  {skillDetail._evolveRes && <div className="mt-2 text-xs text-emerald-300">Evolve: {JSON.stringify(skillDetail._evolveRes).slice(0,300)}</div>}
                  {skillDetail._evolveErr && <div className="mt-2 text-xs text-red-400">{skillDetail._evolveErr}</div>}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
