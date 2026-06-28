/**
 * Intelligence — Headroom + Graphify in one page.
 *
 * - Headroom AI: token-compression savings across all routed agent/LLM calls.
 * - Graphify: the code/knowledge map agents query at runtime (communities,
 *   god nodes, live term lookup).
 * - Agent knowledge store: durable facts agents persist and reference on tasks.
 *
 * All data is best-effort from the AI Market Analyst plugin; the page degrades
 * gracefully if the plugin or graph is unavailable.
 */
import Head from 'next/head'
import { useCallback, useEffect, useState } from 'react'
import { apiClient } from '@/services/api'
import { Network, Sparkles, Search, Brain, Trash2, RefreshCw, Gauge } from 'lucide-react'

export default function IntelligencePage() {
  const [headroom, setHeadroom] = useState<any>(null)
  const [graph, setGraph] = useState<any>(null)
  const [knowledge, setKnowledge] = useState<any[]>([])
  const [agentUsage, setAgentUsage] = useState<any>(null)
  const [term, setTerm] = useState('')
  const [queryResult, setQueryResult] = useState<any>(null)
  const [querying, setQuerying] = useState(false)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [h, g, k, au] = await Promise.all([
        apiClient.aiAnalyst.getHeadroom(30),
        apiClient.aiAnalyst.getGraphOverview(),
        apiClient.aiAnalyst.getKnowledge(),
        apiClient.aiAnalyst.getAiUsageAgents(),
      ])
      setHeadroom(h.data)
      setGraph(g.data)
      setKnowledge(k.data?.items || [])
      setAgentUsage(au.data)
    } catch {
      /* plugin may be unavailable */
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  const runQuery = async () => {
    if (!term.trim()) return
    setQuerying(true)
    try {
      const res = await apiClient.aiAnalyst.queryGraph(term.trim(), 8)
      setQueryResult(res.data)
    } catch {
      setQueryResult(null)
    } finally {
      setQuerying(false)
    }
  }

  const deleteKnowledge = async (id: number) => {
    try {
      await apiClient.aiAnalyst.deleteKnowledge(id)
      setKnowledge((prev) => prev.filter((k) => k.id !== id))
    } catch {
      /* ignore */
    }
  }

  return (
    <>
      <Head><title>Intelligence | TradeBot</title></Head>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Network className="w-7 h-7 text-cyan-400" /> Agent Intelligence
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Headroom token compression &amp; the Graphify knowledge map your agents use to think — working together.
            </p>
          </div>
          <button onClick={load} className="p-2 bg-gray-800 rounded hover:bg-gray-700 transition" title="Refresh">
            <RefreshCw className="w-4 h-4 text-gray-400" />
          </button>
        </div>

        {/* Headroom AI */}
        <div className="bg-gradient-to-br from-emerald-500/10 to-transparent border border-emerald-500/30 rounded-lg p-5">
          <h2 className="font-semibold text-white flex items-center gap-2 mb-3">
            <Sparkles className="w-5 h-5 text-emerald-400" /> Headroom AI — token minimisation
          </h2>
          <p className="text-xs text-gray-400 mb-4 max-w-3xl">
            Every routed agent / LLM call is compressed before it's sent. This is the cumulative saving over the
            last {headroom?.window_days ?? 30} days — fewer characters means fewer tokens spent against your free tiers.
          </p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="Calls compressed" value={(headroom?.calls ?? 0).toLocaleString()} />
            <Stat label="Reduction" value={`${headroom?.reduction_pct ?? 0}%`} accent="text-emerald-400" />
            <Stat label="Chars saved" value={(headroom?.chars_saved ?? 0).toLocaleString()} />
            <Stat label="≈ Tokens saved" value={(headroom?.approx_tokens_saved ?? 0).toLocaleString()} accent="text-cyan-300" />
          </div>
          {headroom && headroom.orig_chars > 0 && (
            <div className="mt-4">
              <div className="flex justify-between text-[11px] text-gray-500 mb-1">
                <span>{headroom.comp_chars.toLocaleString()} sent</span>
                <span>{headroom.orig_chars.toLocaleString()} original</span>
              </div>
              <div className="h-2 bg-gray-900 rounded-full overflow-hidden">
                <div
                  className="h-full bg-emerald-500"
                  style={{ width: `${Math.max(2, 100 - headroom.reduction_pct)}%` }}
                />
              </div>
            </div>
          )}
          {headroom && headroom.reduction_pct === 0 && headroom.calls > 0 && (
            <p className="text-[11px] text-gray-500 mt-3">
              No reduction yet — short single-shot prompts compress little. Savings grow as agents build richer context.
            </p>
          )}
        </div>

        {/* Graphify */}
        <div className="bg-gradient-to-br from-cyan-500/10 to-transparent border border-cyan-500/30 rounded-lg p-5">
          <h2 className="font-semibold text-white flex items-center gap-2 mb-3">
            <Network className="w-5 h-5 text-cyan-400" /> Graphify — code &amp; knowledge map
          </h2>
          {graph?.available ? (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
                <Stat label="Nodes" value={(graph.nodes ?? 0).toLocaleString()} />
                <Stat label="Links" value={(graph.links ?? 0).toLocaleString()} />
                <Stat label="Communities" value={(graph.communities?.length ?? 0).toLocaleString()} accent="text-cyan-300" />
                <Stat label="God nodes" value={(graph.god_nodes?.length ?? 0).toLocaleString()} accent="text-purple-300" />
              </div>

              {/* Runtime query (what agents do) */}
              <div className="bg-gray-900/40 rounded-lg p-4 mb-4">
                <label className="text-xs text-gray-400 flex items-center gap-1.5 mb-2">
                  <Search className="w-3.5 h-3.5" /> Query the map (agents do this at runtime to ground a task)
                </label>
                <div className="flex gap-2">
                  <input
                    value={term}
                    onChange={(e) => setTerm(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && runQuery()}
                    placeholder="e.g. orchestrator, sniper, BTC, signal pipeline"
                    className="flex-1 rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                  />
                  <button
                    onClick={runQuery}
                    disabled={querying || !term.trim()}
                    className="px-3 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 rounded text-sm font-medium transition"
                  >
                    {querying ? '…' : 'Map it'}
                  </button>
                </div>
                {queryResult && (
                  <div className="mt-3 text-xs space-y-2">
                    {queryResult.matches?.length > 0 ? (
                      <>
                        <div>
                          <span className="text-gray-500">Matches:</span>
                          <div className="flex flex-wrap gap-1.5 mt-1">
                            {queryResult.matches.map((m: any, i: number) => (
                              <span key={i} className="px-2 py-0.5 bg-cyan-900/30 border border-cyan-500/30 rounded text-cyan-200">
                                {m.label} {m.community && <span className="text-gray-500">· {m.community}</span>}
                              </span>
                            ))}
                          </div>
                        </div>
                        {queryResult.neighbours?.length > 0 && (
                          <div>
                            <span className="text-gray-500">Relationships:</span>
                            <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-gray-300">
                              {queryResult.neighbours.slice(0, 12).map((n: any, i: number) => (
                                <li key={i}>{n.from} <span className="text-cyan-400">—{n.relation}→</span> {n.to}</li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </>
                    ) : (
                      <span className="text-gray-500">No nodes matched “{queryResult.term}”.</span>
                    )}
                  </div>
                )}
              </div>

              {/* Communities + god nodes */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <h3 className="text-xs font-semibold text-gray-300 mb-2">Top communities</h3>
                  <div className="space-y-1">
                    {(graph.communities || []).slice(0, 10).map((c: any, i: number) => (
                      <div key={i} className="flex justify-between text-xs text-gray-400">
                        <span className="truncate pr-2">{c.name}</span>
                        <span className="text-gray-500">{c.nodes}</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <h3 className="text-xs font-semibold text-gray-300 mb-2">Most-connected (god) nodes</h3>
                  <div className="space-y-1">
                    {(graph.god_nodes || []).slice(0, 10).map((n: any, i: number) => (
                      <div key={i} className="flex justify-between text-xs text-gray-400">
                        <span className="truncate pr-2 font-mono">{n.label}</span>
                        <span className="text-purple-400">{n.degree}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </>
          ) : (
            <p className="text-xs text-amber-300">
              Graphify map not found. Generate it with <code className="text-gray-200">scripts/update-graph.sh</code> or
              run <code className="text-gray-200">graphify .</code> in the repo root.
            </p>
          )}
        </div>

        {/* Agent knowledge store */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-semibold text-white flex items-center gap-2">
              <Brain className="w-5 h-5 text-purple-400" /> Agent knowledge store
            </h2>
            <span className="text-[11px] text-gray-500">{knowledge.length} facts · referenced on future tasks</span>
          </div>
          <p className="text-xs text-gray-400 mb-4 max-w-3xl">
            Insights and decision outcomes agents persist (plus Graphify facts) and inject into future prompts so they
            build on what worked. Higher-weight facts surface first.
          </p>
          {knowledge.length === 0 ? (
            <p className="text-xs text-gray-500">No knowledge stored yet — it accumulates as agents analyse symbols.</p>
          ) : (
            <div className="space-y-2 max-h-96 overflow-y-auto">
              {knowledge.map((k) => (
                <div key={k.id} className="flex items-start justify-between gap-3 bg-gray-900/40 rounded p-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 text-[11px] mb-0.5">
                      <span className="px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 uppercase">{k.kind}</span>
                      {k.symbol && <span className="text-gray-400">{k.symbol}</span>}
                      {k.agent_role && <span className="text-gray-500">· {k.agent_role}</span>}
                      <span className="text-gray-600">· weight {Number(k.weight).toFixed(1)}</span>
                      {k.hits > 0 && <span className="text-gray-600">· {k.hits} hits</span>}
                    </div>
                    {k.title && <div className="text-xs font-medium text-gray-200">{k.title}</div>}
                    <div className="text-xs text-gray-400 break-words">{k.content}</div>
                  </div>
                  <button
                    onClick={() => deleteKnowledge(k.id)}
                    className="p-1 text-gray-500 hover:text-red-400 transition shrink-0"
                    title="Delete"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Per-agent usage recap */}
        {agentUsage?.agents?.length > 0 && (
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <h2 className="font-semibold text-white flex items-center gap-2 mb-3">
              <Gauge className="w-5 h-5 text-cyan-400" /> Token usage by agent (this month)
            </h2>
            <div className="space-y-2">
              {agentUsage.agents.map((a: any) => {
                const max = Math.max(...agentUsage.agents.map((x: any) => x.total_tokens), 1)
                const pct = Math.round((a.total_tokens / max) * 100)
                return (
                  <div key={a.agent_role}>
                    <div className="flex justify-between text-[11px] text-gray-400 mb-0.5">
                      <span>{a.agent_role}</span>
                      <span className="text-cyan-300">{a.total_tokens.toLocaleString()} tok · {a.calls} calls</span>
                    </div>
                    <div className="h-1.5 bg-gray-900 rounded-full overflow-hidden">
                      <div className="h-full bg-cyan-500" style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {loading && <div className="text-center text-gray-500 text-sm py-4">Loading intelligence…</div>}
      </div>
    </>
  )
}

function Stat({ label, value, accent = 'text-white' }: { label: string; value: string; accent?: string }) {
  return (
    <div className="bg-gray-900/50 rounded-lg p-3">
      <div className="text-[11px] text-gray-400">{label}</div>
      <div className={`text-lg font-bold ${accent}`}>{value}</div>
    </div>
  )
}
