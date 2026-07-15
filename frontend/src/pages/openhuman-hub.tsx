/**
 * OpenHuman Hub — Full integration matching the OpenHuman app screenshots.
 * Tabs: Brain (Memory Tree Graph) · Tiny Place · Subconscious · Research
 *       Agents (Jarvis+OpenHuman joint) · Integrations · Workflows
 *       Kronos · SMC · Signals · Settings
 */
import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import dynamic from 'next/dynamic'
import {
  Brain, Bot, Telescope, Crosshair, Zap, Settings, RefreshCw,
  CheckCircle, XCircle, Loader2, AlertTriangle, Search,
  Send, Network, Workflow, Target, BookOpen, Cpu, Globe,
  Activity, Star, Circle, Plus, Trash2, CheckSquare, Square,
  Layers, Link2, Atom, Eye, Clock, Mic, Moon, Lock, Unlock,
  Users, TreePine, Map,
} from 'lucide-react'
import { apiClient } from '@/services/api'
import SignalFeed from '@/components/SignalFeed'
import OpenHumanMascot, { MascotMood } from '@/components/OpenHumanMascot'

const KronosForecastCard = dynamic(() => import('@/components/KronosForecastCard'), { ssr: false })
const MemoryGraph = dynamic(() => import('@/components/MemoryGraph'), { ssr: false })
const TinyPlaceWorld = dynamic(() => import('@/components/TinyPlaceWorld'), { ssr: false })

// ─── Types ─────────────────────────────────────────────────────────────────

interface OpenHumanStatus {
  agentmemory_reachable: boolean
  openhuman_reachable: boolean
  memory_entry_count: number
  message?: string
}

interface MemoryEntry {
  id: number; source: string; symbol?: string
  content: string; tags?: string; synced: boolean; created_at: string
}

interface PaulDecision {
  id: number; symbol: string; action?: string; confidence?: number
  status: string; reasoning?: string; created_at: string
}

interface Goal {
  id: string; title: string; description?: string
  status: 'todo' | 'in_progress' | 'done'
  priority: 'high' | 'medium' | 'low'
  created_at: string
}

interface WorkflowItem {
  id: string; name: string; trigger: string
  status: 'active' | 'paused' | 'draft'
  last_run?: string; runs: number
}

interface Integration {
  name: string; icon: string; connected: boolean
  last_sync?: string; description: string
}

type TabId =
  | 'brain' | 'tinyplace' | 'subconscious' | 'research'
  | 'agents' | 'integrations' | 'workflows'
  | 'kronos' | 'smc' | 'signals' | 'settings'

type BrainSubTab = 'graph' | 'contacts'

interface TabDef { id: TabId; label: string; icon: React.ReactNode }

const TABS: TabDef[] = [
  { id: 'brain',        label: 'Brain',        icon: <Brain     className="w-4 h-4" /> },
  { id: 'tinyplace',    label: 'Tiny Place',   icon: <Map       className="w-4 h-4" /> },
  { id: 'subconscious', label: 'Subconscious', icon: <Moon      className="w-4 h-4" /> },
  { id: 'research',     label: 'Research',     icon: <Telescope className="w-4 h-4" /> },
  { id: 'agents',       label: 'Agents',       icon: <Bot       className="w-4 h-4" /> },
  { id: 'integrations', label: 'Integrations', icon: <Link2     className="w-4 h-4" /> },
  { id: 'workflows',    label: 'Workflows',    icon: <Workflow  className="w-4 h-4" /> },
  { id: 'kronos',       label: 'Kronos',       icon: <Atom      className="w-4 h-4" /> },
  { id: 'smc',          label: 'SMC',          icon: <Crosshair className="w-4 h-4" /> },
  { id: 'signals',      label: 'Signals',      icon: <Zap       className="w-4 h-4" /> },
  { id: 'settings',     label: 'Settings',     icon: <Settings  className="w-4 h-4" /> },
]

const MOCK_INTEGRATIONS: Integration[] = [
  { name: 'Telegram',    icon: '✈️', connected: true,  description: 'Signal + news channels' },
  { name: 'GitHub',      icon: '🐙', connected: false, description: 'Repo context + code memory' },
  { name: 'Gmail',       icon: '📧', connected: false, description: 'Email + calendar' },
  { name: 'Slack',       icon: '💬', connected: false, description: 'Channel messages' },
  { name: 'Notion',      icon: '📄', connected: false, description: 'Notes + databases' },
  { name: 'Linear',      icon: '🔷', connected: false, description: 'Issue tracking' },
  { name: 'Binance',     icon: '🟡', connected: true,  description: 'Portfolio + trade history' },
  { name: 'MT5',         icon: '📊', connected: true,  description: 'Forex positions + signals' },
  { name: 'TradingView', icon: '📈', connected: false, description: 'Chart overlays + alerts' },
  { name: 'Discord',     icon: '🎮', connected: false, description: 'Crypto alpha + signals' },
  { name: 'Twitter/X',   icon: '🐦', connected: false, description: 'Market sentiment' },
  { name: 'Obsidian',    icon: '🔮', connected: false, description: 'Knowledge vault' },
]

const StatusBadge = ({ ok, label }: { ok: boolean; label: string }) => (
  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${ok ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}`}>
    {ok ? <CheckCircle className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
    {label}
  </span>
)

// ─── Page ──────────────────────────────────────────────────────────────────

export default function OpenHumanHubPage() {
  const [activeTab,    setActiveTab]    = useState<TabId>('brain')
  const [brainSubTab,  setBrainSubTab]  = useState<BrainSubTab>('graph')
  const [mascotMood,   setMascotMood]   = useState<MascotMood>('idle')
  const [ohStatus,     setOhStatus]     = useState<OpenHumanStatus | null>(null)
  const [memEntries,   setMemEntries]   = useState<MemoryEntry[]>([])
  const [syncing,      setSyncing]      = useState(false)
  const [syncMsg,      setSyncMsg]      = useState('')
  const [queryText,    setQueryText]    = useState('')
  const [queryResults, setQueryResults] = useState<unknown>(null)
  const [queryLoading, setQueryLoading] = useState(false)
  const [goals, setGoals] = useState<Goal[]>([
    { id: '1', title: 'Optimise BTC entry timing', description: 'Kronos + SMC confluence', status: 'in_progress', priority: 'high',   created_at: new Date().toISOString() },
    { id: '2', title: 'Build forex signal accuracy report',                                 status: 'todo',        priority: 'medium', created_at: new Date().toISOString() },
    { id: '3', title: 'Connect Gmail for market news digest',                               status: 'todo',        priority: 'low',    created_at: new Date().toISOString() },
  ])
  const [newGoalText, setNewGoalText] = useState('')
  const [researchPrompt,  setResearchPrompt]  = useState('')
  const [researchResult,  setResearchResult]  = useState<unknown>(null)
  const [researchLoading, setResearchLoading] = useState(false)
  const [jarvisCmd,     setJarvisCmd]     = useState('')
  const [jarvisResult,  setJarvisResult]  = useState<unknown>(null)
  const [jarvisLoading, setJarvisLoading] = useState(false)
  const [paulDecisions, setPaulDecisions] = useState<PaulDecision[]>([])
  const [jointMode,     setJointMode]     = useState(true)
  const [workflows, setWorkflows] = useState<WorkflowItem[]>([
    { id: 'wf1', name: 'Morning Market Briefing',  trigger: 'cron:07:00', status: 'active', last_run: '6h ago', runs: 42  },
    { id: 'wf2', name: 'Signal → Telegram Alert',  trigger: 'signal:new', status: 'active', last_run: '2h ago', runs: 218 },
    { id: 'wf3', name: 'Portfolio Rebalance Check', trigger: 'cron:12:00', status: 'paused', last_run: '1d ago', runs: 12  },
  ])
  const [proposeFlow,   setProposeFlow]   = useState('')
  const [proposingFlow, setProposingFlow] = useState(false)
  const [smcSymbol,     setSmcSymbol]     = useState('BTC/USDT')
  const [smcOverview,   setSmcOverview]   = useState<unknown>(null)
  const [smcLoading,    setSmcLoading]    = useState(false)
  const [privacyMode,   setPrivacyMode]   = useState(false)
  const [syncInterval,  setSyncInterval]  = useState('20')

  // ── Fetchers ──────────────────────────────────────────────────────────────

  const fetchOhStatus = useCallback(async () => {
    try { const r = await apiClient.openHuman.status(); setOhStatus(r.data as OpenHumanStatus) }
    catch { setOhStatus({ agentmemory_reachable: false, openhuman_reachable: false, memory_entry_count: 0 }) }
  }, [])

  const fetchMemEntries = useCallback(async () => {
    try { const r = await apiClient.openHuman.getEntries(50); setMemEntries(Array.isArray(r.data) ? r.data as MemoryEntry[] : []) }
    catch {}
  }, [])

  const fetchPaulDecisions = useCallback(async () => {
    try {
      const r = await apiClient.agentPaul.getDecisions(20)
      const d = r.data as PaulDecision[] | { decisions?: PaulDecision[] }
      setPaulDecisions(Array.isArray(d) ? d : (d as { decisions?: PaulDecision[] }).decisions ?? [])
    } catch {}
  }, [])

  useEffect(() => {
    fetchOhStatus(); fetchMemEntries(); fetchPaulDecisions()
    const t = setInterval(() => { fetchOhStatus(); fetchMemEntries() }, 30_000)
    return () => clearInterval(t)
  }, [fetchOhStatus, fetchMemEntries, fetchPaulDecisions])

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleSync = async () => {
    setSyncing(true); setSyncMsg(''); setMascotMood('thinking')
    try {
      const r = await apiClient.openHuman.syncMemory()
      setSyncMsg(`Synced ${(r.data as { synced_count?: number }).synced_count ?? 0} items.`)
      setMascotMood('talking')
      await fetchOhStatus(); await fetchMemEntries()
    } catch (e) { setSyncMsg(String(e)); setMascotMood('idle') }
    finally { setSyncing(false); setTimeout(() => setMascotMood('idle'), 3000) }
  }

  const handleQuery = async () => {
    if (!queryText.trim()) return
    setQueryLoading(true); setMascotMood('thinking')
    try { const r = await apiClient.openHuman.queryMemory(queryText); setQueryResults(r.data); setMascotMood('talking') }
    catch { setMascotMood('idle') }
    finally { setQueryLoading(false); setTimeout(() => setMascotMood('idle'), 2000) }
  }

  const handleResearch = async () => {
    if (!researchPrompt.trim()) return
    setResearchLoading(true); setResearchResult(null); setMascotMood('thinking')
    try { const r = await apiClient.openHuman.research(researchPrompt); setResearchResult(r.data); setMascotMood('talking') }
    catch (e) { setResearchResult({ error: String(e) }); setMascotMood('surprised') }
    finally { setResearchLoading(false); setTimeout(() => setMascotMood('idle'), 3000) }
  }

  const handleJarvis = async () => {
    if (!jarvisCmd.trim()) return
    setJarvisLoading(true); setJarvisResult(null); setMascotMood('thinking')
    try {
      let finalCmd = jarvisCmd
      if (jointMode && ohStatus?.agentmemory_reachable) {
        const ctx = memEntries.slice(0, 3).map(e => e.content).join('; ')
        if (ctx) finalCmd = `[Brain context: ${ctx}] ${jarvisCmd}`
      }
      const ac = apiClient as unknown as Record<string, { executeCommand?: (c: string) => Promise<{ data: unknown }> }>
      const r = await ac.jarvis?.executeCommand?.(finalCmd)
      setJarvisResult(r?.data ?? { message: 'Command sent — check Jarvis logs' })
      setMascotMood('talking')
      if (jointMode) { try { await apiClient.openHuman.syncMemory() } catch {} }
    } catch (e) { setJarvisResult({ error: String(e) }); setMascotMood('surprised') }
    finally { setJarvisLoading(false); setTimeout(() => setMascotMood('idle'), 3000) }
  }

  const handleSmc = async () => {
    setSmcLoading(true)
    try {
      const ac = apiClient as unknown as { getSmcOverview?: (p: unknown) => Promise<{ data: unknown }> }
      const r = await ac.getSmcOverview?.({ symbol: smcSymbol, limit: 10 })
      setSmcOverview(r?.data)
    } finally { setSmcLoading(false) }
  }

  const addGoal = () => {
    if (!newGoalText.trim()) return
    setGoals(prev => [{
      id: Date.now().toString(), title: newGoalText.trim(),
      status: 'todo', priority: 'medium', created_at: new Date().toISOString(),
    }, ...prev])
    setNewGoalText('')
  }

  const cycleGoal = (id: string) =>
    setGoals(prev => prev.map(g => g.id !== id ? g : {
      ...g, status: g.status === 'todo' ? 'in_progress' : g.status === 'in_progress' ? 'done' : 'todo',
    }))

  const proposeWorkflow = async () => {
    if (!proposeFlow.trim()) return
    setProposingFlow(true); setMascotMood('thinking')
    await new Promise(r => setTimeout(r, 1200))
    setWorkflows(prev => [{ id: `wf${Date.now()}`, name: proposeFlow, trigger: 'manual', status: 'draft', runs: 0 }, ...prev])
    setProposeFlow(''); setProposingFlow(false); setMascotMood('talking')
    setTimeout(() => setMascotMood('idle'), 2000)
  }

  const prioColor: Record<Goal['priority'], string> = {
    high: 'bg-red-500/20 text-red-300', medium: 'bg-yellow-500/20 text-yellow-300', low: 'bg-gray-500/20 text-gray-400',
  }
  const statusIcon: Record<Goal['status'], React.ReactNode> = {
    todo:        <Square     className="w-4 h-4 text-gray-500" />,
    in_progress: <Circle     className="w-4 h-4 text-blue-400" />,
    done:        <CheckSquare className="w-4 h-4 text-green-400" />,
  }
  const wfColor: Record<WorkflowItem['status'], string> = {
    active: 'text-green-400 bg-green-500/10',
    paused: 'text-yellow-400 bg-yellow-500/10',
    draft:  'text-gray-400 bg-gray-500/10',
  }

  return (
    <>
      <Head><title>OpenHuman — TradeBot</title></Head>
      <div className="p-4 max-w-7xl mx-auto space-y-4">

        {/* ── Header ── */}
        <div className="flex items-center justify-between flex-wrap gap-4">
          <div className="flex items-center gap-4">
            <OpenHumanMascot mood={mascotMood} size={72} showLabel />
            <div>
              <h1 className="text-2xl font-bold text-white flex items-center gap-2">
                OpenHuman
                <span className="text-xs font-normal px-2 py-0.5 rounded-full bg-purple-500/20 text-purple-300 border border-purple-500/30">Early Beta</span>
              </h1>
              <p className="text-sm text-gray-400 mt-0.5">Personal AI brain · Memory Tree · Agent Orchestrator · Tiny Place</p>
              <div className="flex items-center gap-3 mt-1.5 flex-wrap">
                <StatusBadge ok={ohStatus?.agentmemory_reachable ?? false} label="Memory Brain" />
                <StatusBadge ok={ohStatus?.openhuman_reachable   ?? false} label="OpenHuman :7788" />
                {(ohStatus?.memory_entry_count ?? 0) > 0 && (
                  <span className="text-xs text-gray-500">{ohStatus!.memory_entry_count} memory entries</span>
                )}
              </div>
            </div>
          </div>
          <button onClick={() => { fetchOhStatus(); fetchMemEntries(); fetchPaulDecisions() }}
            className="p-2 rounded-lg hover:bg-gray-700 text-gray-400 hover:text-white transition-colors">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>

        {/* ── Tab Bar ── */}
        <div className="flex gap-1.5 flex-wrap border-b border-gray-700/50 pb-2">
          {TABS.map(tab => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                activeTab === tab.id
                  ? 'bg-purple-600 text-white shadow-lg shadow-purple-500/20'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white'
              }`}>
              {tab.icon}<span className="hidden sm:inline">{tab.label}</span>
            </button>
          ))}
        </div>

        {/* ════ BRAIN ════ */}
        {activeTab === 'brain' && (
          <div className="space-y-4">
            {/* Sub-tabs + buttons */}
            <div className="flex items-center justify-between flex-wrap gap-2">
              <div className="flex gap-2">
                {(['graph', 'contacts'] as BrainSubTab[]).map(st => (
                  <button key={st} onClick={() => setBrainSubTab(st)}
                    className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                      brainSubTab === st ? 'bg-white text-black' : 'bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-white border border-gray-700'
                    }`}>
                    {st === 'graph' ? 'Trees' : 'Contacts'}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <button onClick={handleSync} disabled={syncing}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-xs transition-colors disabled:opacity-50">
                  {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}Reset Memory
                </button>
                <button className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-xs transition-colors">
                  <TreePine className="w-3.5 h-3.5" />Reset Memory Tree
                </button>
                <button onClick={() => { fetchOhStatus(); fetchMemEntries() }}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-xs transition-colors">
                  <RefreshCw className="w-3.5 h-3.5" />Refresh
                </button>
                <button className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-xs transition-colors">
                  <Eye className="w-3.5 h-3.5" />View Vault
                </button>
                <button className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold transition-colors">
                  <TreePine className="w-3.5 h-3.5" />Build Summary Trees
                </button>
              </div>
            </div>

            {brainSubTab === 'graph' ? (
              <div className="bg-gray-900/50 rounded-xl border border-gray-700/40 overflow-hidden">
                <MemoryGraph height={500} />
              </div>
            ) : (
              <div className="bg-gray-800/60 rounded-xl p-6 border border-gray-700/40 text-center">
                <Users className="w-10 h-10 text-gray-600 mx-auto mb-3" />
                <h3 className="text-white font-semibold">Contacts</h3>
                <p className="text-xs text-gray-400 mt-1">Agent contacts from the memory graph appear here.</p>
                <p className="text-xs text-gray-600 mt-2">Connect OpenHuman desktop to populate contacts.</p>
              </div>
            )}

            {/* Side sections */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-2">
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">MEMORY</h3>
                {[
                  { icon: <Atom     className="w-4 h-4 text-purple-400" />, label: 'Graph',   count: memEntries.length },
                  { icon: <Target   className="w-4 h-4 text-blue-400"   />, label: 'Goals',   count: goals.length      },
                  { icon: <BookOpen className="w-4 h-4 text-green-400"  />, label: 'Sources', count: MOCK_INTEGRATIONS.filter(i => i.connected).length },
                  { icon: <RefreshCw className="w-4 h-4 text-yellow-400" />, label: 'Sync',  count: null },
                ].map(item => (
                  <div key={item.label} className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-gray-700/40 cursor-pointer">
                    {item.icon}<span className="text-sm text-white flex-1">{item.label}</span>
                    {item.count != null && <span className="text-xs text-gray-500">{item.count}</span>}
                  </div>
                ))}
              </div>
              <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-2">
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">KNOWLEDGE &amp; MEMORY</h3>
                {[
                  { icon: <Brain    className="w-4 h-4 text-purple-400" />, label: 'Intelligence'      },
                  { icon: <Eye      className="w-4 h-4 text-blue-400"   />, label: 'Memory Inspection' },
                  { icon: <Cpu      className="w-4 h-4 text-green-400"  />, label: 'Debug Panels'      },
                  { icon: <Activity className="w-4 h-4 text-yellow-400" />, label: 'Analysis views'    },
                ].map(item => (
                  <div key={item.label} className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-gray-700/40 cursor-pointer">
                    {item.icon}<span className="text-sm text-white flex-1">{item.label}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Memory search */}
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Search className="w-4 h-4 text-blue-400" />Memory Search</h3>
              <div className="flex gap-2">
                <input value={queryText} onChange={e => setQueryText(e.target.value)} onKeyDown={e => e.key === 'Enter' && handleQuery()}
                  placeholder="Search memory… e.g. 'BTC signals last week'"
                  className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500" />
                <button onClick={handleQuery} disabled={queryLoading} className="px-3 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded-lg disabled:opacity-50">
                  {queryLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Search className="w-4 h-4" />}
                </button>
              </div>
              {queryResults != null && (
                <pre className="text-xs text-gray-300 bg-gray-900/50 rounded-lg p-3 max-h-40 overflow-y-auto whitespace-pre-wrap">{JSON.stringify(queryResults, null, 2)}</pre>
              )}
              {syncMsg && <div className="text-xs text-green-400 flex items-center gap-1"><CheckCircle className="w-3.5 h-3.5" />{syncMsg}</div>}
            </div>
          </div>
        )}

        {/* ════ TINY PLACE ════ */}
        {activeTab === 'tinyplace' && (
          <div className="space-y-3">
            <div className="bg-purple-500/10 border border-purple-500/20 rounded-xl px-4 py-2.5 flex items-center gap-3">
              <Users className="w-4 h-4 text-purple-400 shrink-0" />
              <p className="text-xs text-gray-300">
                <strong className="text-purple-300">Tiny Place</strong> is a social network for AI agents.
                Use OpenHuman to interact, find and post jobs, trade, and grow together.
              </p>
            </div>
            <div style={{ height: 580 }}>
              <TinyPlaceWorld onSelectRoom={() => {}} />
            </div>
          </div>
        )}

        {/* ════ SUBCONSCIOUS ════ */}
        {activeTab === 'subconscious' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-5 border border-gray-700/40">
              <div className="flex items-center gap-3 mb-4">
                <div className={`w-3 h-3 rounded-full ${ohStatus?.agentmemory_reachable ? 'bg-green-400 animate-pulse' : 'bg-gray-600'}`} />
                <h2 className="text-base font-bold text-white">Subconscious Loop</h2>
                <span className={`text-xs px-2 py-0.5 rounded-full ${ohStatus?.agentmemory_reachable ? 'bg-green-500/20 text-green-400' : 'bg-gray-500/20 text-gray-400'}`}>
                  {ohStatus?.agentmemory_reachable ? '🧠 Thinking in background' : '💤 Inactive'}
                </span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {[
                  { icon: <Activity className="w-4 h-4 text-blue-400"   />, label: 'Tick interval', value: '60s'   },
                  { icon: <Target   className="w-4 h-4 text-purple-400" />, label: 'Active goals',  value: goals.filter(g => g.status === 'in_progress').length },
                  { icon: <Star     className="w-4 h-4 text-yellow-400" />, label: 'Done today',    value: goals.filter(g => g.status === 'done').length         },
                  { icon: <Moon     className="w-4 h-4 text-indigo-400" />, label: 'Dream state',   value: 'Ready' },
                ].map((c, i) => (
                  <div key={i} className="bg-gray-900/50 rounded-xl p-3 flex flex-col gap-1">
                    <div className="flex items-center gap-1.5">{c.icon}<span className="text-xs text-gray-400">{c.label}</span></div>
                    <span className="text-white font-semibold">{c.value}</span>
                  </div>
                ))}
              </div>
            </div>
            {/* Goals kanban */}
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <div className="flex items-center gap-2"><Target className="w-4 h-4 text-purple-400" /><h3 className="text-sm font-semibold text-white">Goals &amp; Todos</h3></div>
              <div className="flex gap-2">
                <input value={newGoalText} onChange={e => setNewGoalText(e.target.value)} onKeyDown={e => e.key === 'Enter' && addGoal()}
                  placeholder="Add a goal…" className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500" />
                <button onClick={addGoal} className="px-3 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded-lg"><Plus className="w-4 h-4" /></button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {(['todo','in_progress','done'] as Goal['status'][]).map(col => (
                  <div key={col} className="space-y-2">
                    <div className="text-xs font-semibold text-gray-400 uppercase tracking-wide px-1">
                      {col === 'todo' ? '📋 Backlog' : col === 'in_progress' ? '🔄 In Progress' : '✅ Done'}
                      <span className="ml-1 text-gray-600">({goals.filter(g => g.status === col).length})</span>
                    </div>
                    {goals.filter(g => g.status === col).map(g => (
                      <div key={g.id} className="bg-gray-900/60 rounded-xl p-3 border border-gray-700/40 group hover:border-purple-500/40 transition-colors">
                        <div className="flex items-start gap-2">
                          <button onClick={() => cycleGoal(g.id)} className="mt-0.5 shrink-0">{statusIcon[g.status]}</button>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm text-white font-medium leading-tight">{g.title}</p>
                            {g.description && <p className="text-xs text-gray-500 mt-1">{g.description}</p>}
                            <span className={`mt-1.5 inline-block text-xs px-1.5 py-0.5 rounded ${prioColor[g.priority]}`}>{g.priority}</span>
                          </div>
                          <button onClick={() => setGoals(p => p.filter(x => x.id !== g.id))} className="opacity-0 group-hover:opacity-100 text-gray-600 hover:text-red-400 transition-all shrink-0"><Trash2 className="w-3.5 h-3.5" /></button>
                        </div>
                      </div>
                    ))}
                    {goals.filter(g => g.status === col).length === 0 && (
                      <div className="text-center py-4 border border-dashed border-gray-700/50 rounded-xl text-xs text-gray-600">Empty</div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ════ RESEARCH ════ */}
        {activeTab === 'research' && (
          <div className="bg-gray-800/60 rounded-xl p-5 border border-gray-700/40 space-y-4">
            <div className="flex items-center gap-2"><Telescope className="w-5 h-5 text-purple-400" /><h2 className="text-base font-bold text-white">Deep Research Console</h2></div>
            <p className="text-xs text-gray-400">SuperContext sweeps your memory and the web before every query. Trading context is auto-loaded.</p>
            <textarea value={researchPrompt} onChange={e => setResearchPrompt(e.target.value)} rows={3}
              placeholder="Research a topic, analyse a coin, explain a chart pattern…"
              className="w-full bg-gray-900 border border-gray-700 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500 resize-none" />
            <div className="flex items-center gap-3 flex-wrap">
              <button onClick={handleResearch} disabled={researchLoading || !researchPrompt.trim()}
                className="flex items-center gap-2 px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded-lg text-sm disabled:opacity-50 transition-colors">
                {researchLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                {researchLoading ? 'Researching…' : 'Research'}
              </button>
              {!ohStatus?.openhuman_reachable && (
                <span className="text-xs text-yellow-400 flex items-center gap-1"><AlertTriangle className="w-3.5 h-3.5" />Using Jarvis fallback</span>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {['What are the strongest SMC signals right now?','Analyse BTC market structure','XAUUSD outlook tonight?','Summarise my trading this week'].map((p, i) => (
                <button key={i} onClick={() => setResearchPrompt(p)} className="text-xs bg-gray-700 hover:bg-gray-600 text-gray-300 px-3 py-1.5 rounded-lg">{p}</button>
              ))}
            </div>
            {researchResult != null && (
              <div className="bg-gray-900/50 rounded-xl p-4 border border-gray-700/40">
                <div className="flex items-center gap-2 mb-2"><Brain className="w-4 h-4 text-purple-400" /><span className="text-xs font-semibold text-gray-300">Result</span></div>
                <pre className="text-xs text-gray-300 whitespace-pre-wrap max-h-96 overflow-y-auto leading-relaxed">
                  {typeof researchResult === 'string' ? researchResult : JSON.stringify(researchResult, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}

        {/* ════ AGENTS (JARVIS + OPENHUMAN) ════ */}
        {activeTab === 'agents' && (
          <div className="space-y-4">
            <div className="bg-purple-500/10 border border-purple-500/20 rounded-xl p-4 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="flex -space-x-2">
                  <div className="w-8 h-8 rounded-full bg-cyan-600 flex items-center justify-center text-xs font-bold text-white border-2 border-gray-900">J</div>
                  <div className="w-8 h-8 rounded-full bg-purple-600 flex items-center justify-center text-xs font-bold text-white border-2 border-gray-900">OH</div>
                </div>
                <div>
                  <p className="text-sm font-semibold text-white">JARVIS + OpenHuman Joint Mode</p>
                  <p className="text-xs text-gray-400">Enriches every Jarvis command with Memory Tree context for deeper, smarter analysis</p>
                </div>
              </div>
              <button onClick={() => setJointMode(v => !v)}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${jointMode ? 'bg-purple-600' : 'bg-gray-600'}`}>
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${jointMode ? 'translate-x-6' : 'translate-x-1'}`} />
              </button>
            </div>

            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <div className="flex items-center gap-2">
                <Bot className="w-5 h-5 text-cyan-400" /><h2 className="text-sm font-bold text-white">JARVIS Brain Interface</h2>
                <span className="text-xs text-gray-500 ml-auto">{jointMode ? '🔗 + Memory Tree context' : 'Jarvis only'}</span>
              </div>
              {jointMode && memEntries.length > 0 && (
                <div className="bg-purple-500/10 border border-purple-500/20 rounded-lg px-3 py-2 text-xs text-purple-300">
                  <span className="font-semibold">Memory context loaded:</span>{' '}
                  {memEntries.slice(0,3).map(e => e.source).join(', ')}
                  {memEntries.length > 3 && ` and ${memEntries.length - 3} more`} will enrich this query
                </div>
              )}
              <div className="flex gap-2">
                <input value={jarvisCmd} onChange={e => setJarvisCmd(e.target.value)} onKeyDown={e => e.key === 'Enter' && handleJarvis()}
                  placeholder={jointMode ? 'Ask with memory context…' : 'analyze BTC, forecast ETH, status…'}
                  className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-cyan-500" />
                <button onClick={handleJarvis} disabled={jarvisLoading}
                  className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 text-white rounded-lg text-sm disabled:opacity-50 flex items-center gap-1.5 transition-colors">
                  {jarvisLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Mic className="w-4 h-4" />}Ask
                </button>
              </div>
              <div className="flex flex-wrap gap-2">
                {['status','analyze BTC/USDT','forecast ETH/USDT','open positions','top signals','what signals look best?','summarise my brain context'].map(cmd => (
                  <button key={cmd} onClick={() => setJarvisCmd(cmd)} className="text-xs bg-gray-700 hover:bg-gray-600 text-cyan-300 px-3 py-1 rounded-lg transition-colors">{cmd}</button>
                ))}
              </div>
              {jarvisResult != null && (
                <div className="bg-gray-900/50 rounded-xl p-4 border border-gray-700/40">
                  <div className="flex items-center gap-2 mb-2">
                    <span className="text-xs text-cyan-400 font-semibold">JARVIS</span>
                    {jointMode && <><span className="text-gray-600">+</span><span className="text-xs text-purple-400 font-semibold">Memory</span></>}
                  </div>
                  <pre className="text-xs text-gray-300 bg-gray-950/50 rounded-lg p-3 max-h-64 overflow-y-auto whitespace-pre-wrap">
                    {typeof jarvisResult === 'string' ? jarvisResult : JSON.stringify(jarvisResult, null, 2)}
                  </pre>
                </div>
              )}
            </div>

            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-bold text-white flex items-center gap-2"><Zap className="w-4 h-4 text-orange-400" />Agent Paul — Autonomous Decisions</h2>
                <button onClick={fetchPaulDecisions} className="text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1"><RefreshCw className="w-3 h-3" />Refresh</button>
              </div>
              {paulDecisions.length === 0 ? (
                <div className="text-center py-6"><Cpu className="w-8 h-8 text-gray-600 mx-auto mb-2" /><p className="text-xs text-gray-500">No decisions yet. Agent Paul is watching the markets.</p></div>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto">
                  {paulDecisions.map((d, i) => (
                    <div key={d.id ?? i} className="bg-gray-900/50 rounded-xl p-3 border border-gray-700/30">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-semibold text-white">{d.symbol}</span>
                        {d.action && <span className="text-xs px-2 py-0.5 rounded bg-blue-500/20 text-blue-300">{d.action}</span>}
                        {d.confidence != null && <span className="text-xs text-gray-400">{Math.round(d.confidence * 100)}% conf.</span>}
                        <span className="ml-auto text-xs text-gray-600">{new Date(d.created_at).toLocaleTimeString()}</span>
                      </div>
                      {d.reasoning && <p className="text-xs text-gray-400">{d.reasoning}</p>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ════ INTEGRATIONS ════ */}
        {activeTab === 'integrations' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40">
              <div className="flex items-center gap-2 mb-3">
                <Network className="w-5 h-5 text-blue-400" /><h2 className="text-sm font-bold text-white">Connected Integrations</h2>
                <span className="ml-auto text-xs text-gray-500">{MOCK_INTEGRATIONS.filter(i => i.connected).length}/{MOCK_INTEGRATIONS.length} active</span>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
                {MOCK_INTEGRATIONS.map((intg, i) => (
                  <div key={i} className={`rounded-xl p-3 border transition-colors cursor-pointer ${intg.connected ? 'bg-green-500/10 border-green-500/30 hover:border-green-400/50' : 'bg-gray-900/50 border-gray-700/40 hover:border-gray-600/60'}`}>
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-lg">{intg.icon}</span><span className="text-sm font-medium text-white">{intg.name}</span>
                      {intg.connected && <CheckCircle className="w-3.5 h-3.5 text-green-400 ml-auto" />}
                    </div>
                    <p className="text-xs text-gray-500">{intg.description}</p>
                    {!intg.connected && <p className="text-xs text-blue-400 mt-1.5">Connect via OpenHuman →</p>}
                  </div>
                ))}
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40">
              <div className="flex items-center gap-2 mb-3"><Cpu className="w-5 h-5 text-purple-400" /><h2 className="text-sm font-bold text-white">MCP Servers</h2></div>
              <div className="space-y-2">
                {[
                  { name: 'TradeBot Core',            url: '/api/v1/plugins/openhuman/mcp/sse', status: 'connected'    },
                  { name: 'Vibe Trading',             url: '/api/v1/plugins/vibe-trading/mcp',  status: 'available'    },
                  { name: 'OpenHuman Desktop',        url: 'http://127.0.0.1:7788',             status: ohStatus?.openhuman_reachable ? 'connected' : 'offline' },
                  { name: '5,000+ Community Servers', url: 'Configure via OpenHuman app',         status: 'via-openhuman' },
                ].map((s, i) => (
                  <div key={i} className="flex items-center gap-3 bg-gray-900/50 rounded-lg px-3 py-2.5">
                    <Cpu className="w-4 h-4 text-gray-400 shrink-0" />
                    <div className="flex-1 min-w-0"><p className="text-sm text-white font-medium">{s.name}</p><code className="text-xs text-gray-500 truncate block">{s.url}</code></div>
                    <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${s.status === 'connected' ? 'bg-green-500/20 text-green-400' : s.status === 'offline' ? 'bg-red-500/20 text-red-400' : s.status === 'via-openhuman' ? 'bg-purple-500/20 text-purple-400' : 'bg-blue-500/20 text-blue-400'}`}>{s.status}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ════ WORKFLOWS ════ */}
        {activeTab === 'workflows' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <div className="flex items-center gap-2"><Workflow className="w-5 h-5 text-blue-400" /><h2 className="text-sm font-bold text-white">Propose Workflow</h2></div>
              <p className="text-xs text-gray-400">Describe an automation in plain English. The agent will propose a durable workflow graph.</p>
              <div className="flex gap-2">
                <input value={proposeFlow} onChange={e => setProposeFlow(e.target.value)} onKeyDown={e => e.key === 'Enter' && proposeWorkflow()}
                  placeholder="e.g. 'Send me a Telegram summary of top signals every morning at 7am'"
                  className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-blue-500" />
                <button onClick={proposeWorkflow} disabled={proposingFlow}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm disabled:opacity-50 flex items-center gap-1.5 transition-colors">
                  {proposingFlow ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}Propose
                </button>
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Layers className="w-4 h-4 text-gray-400" />Active Workflows</h3>
              {workflows.map(wf => (
                <div key={wf.id} className="flex items-center gap-3 bg-gray-900/50 rounded-xl p-3 border border-gray-700/30">
                  <div className={`w-2 h-2 rounded-full shrink-0 ${wf.status === 'active' ? 'bg-green-400 animate-pulse' : wf.status === 'paused' ? 'bg-yellow-400' : 'bg-gray-500'}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2"><p className="text-sm font-medium text-white">{wf.name}</p><span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${wfColor[wf.status]}`}>{wf.status}</span></div>
                    <div className="flex items-center gap-3 mt-0.5 text-xs text-gray-500">
                      <span>⚡ {wf.trigger}</span>{wf.last_run && <span>🕐 {wf.last_run}</span>}<span>🔄 {wf.runs} runs</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ════ KRONOS ════ */}
        {activeTab === 'kronos' && (
          <div className="space-y-4">
            {['BTC/USDT','ETH/USDT','XAU/USD'].map(sym => (
              <div key={sym} className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40"><KronosForecastCard symbol={sym} /></div>
            ))}
          </div>
        )}

        {/* ════ SMC ════ */}
        {activeTab === 'smc' && (
          <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
            <div className="flex items-center gap-2"><Crosshair className="w-5 h-5 text-red-400" /><h2 className="text-sm font-bold text-white">SMC Sniper Analysis</h2></div>
            <div className="flex gap-2">
              <input value={smcSymbol} onChange={e => setSmcSymbol(e.target.value)} placeholder="BTC/USDT"
                className="w-36 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-red-500" />
              <button onClick={handleSmc} disabled={smcLoading}
                className="px-4 py-2 bg-red-600 hover:bg-red-500 text-white rounded-lg text-sm disabled:opacity-50 transition-colors">
                {smcLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Analyse'}
              </button>
            </div>
            {smcOverview != null && (
              <pre className="text-xs text-gray-300 bg-gray-900/50 rounded-xl p-4 max-h-96 overflow-y-auto whitespace-pre-wrap border border-gray-700/40">{JSON.stringify(smcOverview, null, 2)}</pre>
            )}
          </div>
        )}

        {/* ════ SIGNALS ════ */}
        {activeTab === 'signals' && (
          <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40">
            <h2 className="text-sm font-bold text-white flex items-center gap-2 mb-4"><Zap className="w-4 h-4 text-yellow-400" />Live Signal Feed</h2>
            <SignalFeed />
          </div>
        )}

        {/* ════ SETTINGS ════ */}
        {activeTab === 'settings' && (
          <div className="space-y-4">
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  {privacyMode ? <Lock className="w-5 h-5 text-green-400" /> : <Unlock className="w-5 h-5 text-gray-400" />}
                  <div>
                    <h3 className="text-sm font-semibold text-white">Privacy Mode</h3>
                    <p className="text-xs text-gray-400">All inference runs locally. No data leaves your machine.</p>
                  </div>
                </div>
                <button onClick={() => setPrivacyMode(v => !v)}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${privacyMode ? 'bg-green-600' : 'bg-gray-600'}`}>
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${privacyMode ? 'translate-x-6' : 'translate-x-1'}`} />
                </button>
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Clock className="w-4 h-4 text-blue-400" />Auto-Fetch Interval</h3>
              <div className="flex items-center gap-3">
                <input type="number" value={syncInterval} onChange={e => setSyncInterval(e.target.value)} min={5} max={120}
                  className="w-24 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500" />
                <span className="text-sm text-gray-400">minutes</span>
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Globe className="w-4 h-4 text-purple-400" />OpenHuman Setup</h3>
              {[
                { label: 'Open the OpenHuman desktop app (memory brain activates automatically)', cmd: 'open -a OpenHuman  # macOS — just launch the app' },
                { label: 'Install OpenHuman desktop (macOS)',            cmd: 'brew tap tinyhumansai/core && brew install openhuman' },
                { label: 'Install OpenHuman (Linux / other)',            cmd: 'curl -fsSL https://tinyhumans.ai/install.sh | sh'      },
              ].map((s, i) => (
                <div key={i}>
                  <p className="text-xs text-gray-400 mb-1">{s.label}</p>
                  <code className="block text-xs text-green-400 bg-green-500/5 rounded px-3 py-2 font-mono select-all">{s.cmd}</code>
                </div>
              ))}
            </div>
            <div className="bg-gray-800/60 rounded-xl p-4 border border-gray-700/40 space-y-3">
              <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Atom className="w-4 h-4 text-purple-400" />Mascot Preview — 6 Mood States</h3>
              <div className="flex flex-wrap gap-4">
                {(['idle','thinking','listening','talking','dreaming','surprised'] as MascotMood[]).map(mood => (
                  <div key={mood} className="flex flex-col items-center gap-1">
                    <OpenHumanMascot mood={mood} size={64} showLabel />
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

      </div>
    </>
  )
}
