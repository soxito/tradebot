import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient } from '@/services/api'
import {
  Bot,
  Plus,
  Trash2,
  Edit3,
  Power,
  Loader2,
  AlertTriangle,
  Save,
  X,
  Settings,
  ChevronDown,
  ChevronUp,
} from 'lucide-react'
import { formatDateZA } from '@/utils/datetime'

interface Agent {
  id: number
  name: string
  slug: string
  description: string | null
  role_type: string
  is_enabled: boolean
  model: string
  reasoning_effort: string
  verbosity: string
  max_output_tokens: number | null
  instruments_json: string[] | null
  timeframes_json: string[] | null
  indicators_json: string[] | null
  allowed_actions: string
  version: number
  created_at: string
}

interface AgentForm {
  name: string
  slug: string
  description: string
  role_type: string
  model: string
  reasoning_effort: string
  system_prompt: string
  instruments: string
  timeframes: string
  indicators: string
}

const MODELS = ['gpt-4o', 'gpt-4o-mini', 'o3', 'o4-mini']
const EFFORTS = ['low', 'medium', 'high']

export default function AIAgentsAdminPage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [form, setForm] = useState<AgentForm>({
    name: '', slug: '', description: '', role_type: 'custom',
    model: 'gpt-4o', reasoning_effort: 'medium', system_prompt: '',
    instruments: '', timeframes: 'M15,H1,H4,D1',
    indicators: 'RSI,EMA_20,EMA_50,ATR,VWAP',
  })

  const fetchAgents = useCallback(async () => {
    try {
      setLoading(true)
      const res = await apiClient.aiAnalyst.getAgents()
      setAgents(res.data)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchAgents() }, [fetchAgents])

  const handleCreate = async () => {
    try {
      await apiClient.aiAnalyst.createAgent({
        name: form.name,
        slug: form.slug,
        description: form.description || undefined,
        role_type: form.role_type,
        model: form.model,
        reasoning_effort: form.reasoning_effort,
        system_prompt: form.system_prompt || undefined,
        instruments_json: form.instruments ? form.instruments.split(',').map(s => s.trim()) : undefined,
        timeframes_json: form.timeframes ? form.timeframes.split(',').map(s => s.trim()) : undefined,
        indicators_json: form.indicators ? form.indicators.split(',').map(s => s.trim()) : undefined,
      })
      setShowCreate(false)
      setForm({ name: '', slug: '', description: '', role_type: 'custom', model: 'gpt-4o', reasoning_effort: 'medium', system_prompt: '', instruments: '', timeframes: 'M15,H1,H4,D1', indicators: 'RSI,EMA_20,EMA_50,ATR,VWAP' })
      await fetchAgents()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const handleToggle = async (id: number) => {
    try {
      await apiClient.aiAnalyst.toggleAgent(id)
      await fetchAgents()
    } catch (e: any) {
      setError(e.message)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this agent?')) return
    try {
      await apiClient.aiAnalyst.deleteAgent(id)
      await fetchAgents()
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <>
      <Head><title>AI Agents Admin | TradeBot</title></Head>
      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Bot className="w-6 h-6 text-purple-400" />
            <h1 className="text-2xl font-bold text-white">AI Agent Profiles</h1>
          </div>
          <button onClick={() => setShowCreate(!showCreate)}
            className="flex items-center gap-2 px-4 py-2 bg-purple-600/20 text-purple-400 rounded-lg hover:bg-purple-600/30 transition-colors">
            <Plus className="w-4 h-4" /> New Agent
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm">
            <AlertTriangle className="w-4 h-4" /> {error}
            <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Create Form */}
        {showCreate && (
          <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl p-5 space-y-4">
            <h3 className="text-white font-medium">Create Agent Profile</h3>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <label className="text-gray-400 text-xs block mb-1">Name</label>
                <input value={form.name} onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Slug (lowercase, hyphens)</label>
                <input value={form.slug} onChange={e => setForm(p => ({ ...p, slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Role Type</label>
                <input value={form.role_type} onChange={e => setForm(p => ({ ...p, role_type: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Model</label>
                <select value={form.model} onChange={e => setForm(p => ({ ...p, model: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm">
                  {MODELS.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Reasoning Effort</label>
                <select value={form.reasoning_effort} onChange={e => setForm(p => ({ ...p, reasoning_effort: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm">
                  {EFFORTS.map(e => <option key={e} value={e}>{e}</option>)}
                </select>
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Instruments (comma-separated)</label>
                <input value={form.instruments} onChange={e => setForm(p => ({ ...p, instruments: e.target.value }))}
                  placeholder="BTCUSDT,ETHUSDT,XAUUSD"
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Timeframes</label>
                <input value={form.timeframes} onChange={e => setForm(p => ({ ...p, timeframes: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              </div>
              <div>
                <label className="text-gray-400 text-xs block mb-1">Indicators</label>
                <input value={form.indicators} onChange={e => setForm(p => ({ ...p, indicators: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
              </div>
            </div>
            <div>
              <label className="text-gray-400 text-xs block mb-1">Description</label>
              <input value={form.description} onChange={e => setForm(p => ({ ...p, description: e.target.value }))}
                className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            </div>
            <div>
              <label className="text-gray-400 text-xs block mb-1">System Prompt (optional)</label>
              <textarea value={form.system_prompt} onChange={e => setForm(p => ({ ...p, system_prompt: e.target.value }))}
                rows={4} className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white text-sm" />
            </div>
            <div className="flex gap-2">
              <button onClick={handleCreate} className="flex items-center gap-2 px-4 py-2 bg-purple-600 text-white rounded text-sm hover:bg-purple-700">
                <Save className="w-4 h-4" /> Create
              </button>
              <button onClick={() => setShowCreate(false)} className="px-4 py-2 bg-gray-700 text-gray-300 rounded text-sm hover:bg-gray-600">Cancel</button>
            </div>
          </div>
        )}

        {/* Agent Cards */}
        <div className="grid gap-4">
          {agents.map(agent => (
            <div key={agent.id} className="bg-gray-800/50 border border-gray-700/50 rounded-xl p-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <Bot className={`w-5 h-5 ${agent.is_enabled ? 'text-purple-400' : 'text-gray-600'}`} />
                  <div>
                    <span className="text-white font-medium">{agent.name}</span>
                    <span className="text-gray-500 text-xs ml-2">v{agent.version}</span>
                  </div>
                  <span className="text-gray-500 text-xs px-2 py-0.5 bg-gray-900 rounded">{agent.model}</span>
                  <span className="text-gray-500 text-xs px-2 py-0.5 bg-gray-900 rounded">{agent.reasoning_effort}</span>
                  <span className="text-gray-500 text-xs px-2 py-0.5 bg-gray-900 rounded">{agent.role_type}</span>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => handleToggle(agent.id)}
                    className={`p-1.5 rounded ${agent.is_enabled ? 'text-green-400 hover:bg-green-900/30' : 'text-gray-500 hover:bg-gray-700'}`}>
                    <Power className="w-4 h-4" />
                  </button>
                  <button onClick={() => setExpanded(expanded === agent.id ? null : agent.id)}
                    className="p-1.5 rounded text-gray-400 hover:bg-gray-700">
                    {expanded === agent.id ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                  </button>
                  <button onClick={() => handleDelete(agent.id)}
                    className="p-1.5 rounded text-red-400 hover:bg-red-900/30">
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>

              {agent.description && (
                <p className="text-gray-400 text-sm mt-1 ml-8">{agent.description}</p>
              )}

              {expanded === agent.id && (
                <div className="mt-3 ml-8 space-y-2 text-sm">
                  {agent.instruments_json && (
                    <div className="flex gap-1 flex-wrap">
                      <span className="text-gray-500 text-xs w-20">Instruments:</span>
                      {agent.instruments_json.map(i => (
                        <span key={i} className="px-2 py-0.5 bg-gray-900 rounded text-xs text-gray-300">{i}</span>
                      ))}
                    </div>
                  )}
                  {agent.timeframes_json && (
                    <div className="flex gap-1 flex-wrap">
                      <span className="text-gray-500 text-xs w-20">Timeframes:</span>
                      {agent.timeframes_json.map(t => (
                        <span key={t} className="px-2 py-0.5 bg-gray-900 rounded text-xs text-gray-300">{t}</span>
                      ))}
                    </div>
                  )}
                  {agent.indicators_json && (
                    <div className="flex gap-1 flex-wrap">
                      <span className="text-gray-500 text-xs w-20">Indicators:</span>
                      {agent.indicators_json.map(ind => (
                        <span key={ind} className="px-2 py-0.5 bg-gray-900 rounded text-xs text-gray-300">{ind}</span>
                      ))}
                    </div>
                  )}
                  <div className="text-gray-500 text-xs">
                    Allowed actions: {agent.allowed_actions} | Created: {formatDateZA(agent.created_at)}
                  </div>
                </div>
              )}
            </div>
          ))}

          {agents.length === 0 && !loading && (
            <div className="text-center text-gray-500 py-12">No agent profiles yet. Create one above.</div>
          )}
        </div>

        {loading && (
          <div className="flex justify-center py-8"><Loader2 className="w-6 h-6 animate-spin text-purple-400" /></div>
        )}
      </div>
    </>
  )
}
