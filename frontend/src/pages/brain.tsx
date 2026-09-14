import Head from 'next/head'
import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { apiClient } from '@/services/api'
import {
  Brain, Sparkles, RefreshCw, Search, Users, Layers, Atom,
  Network, Database, Globe, BookOpen, Cpu, Shield, Zap,
  ExternalLink, Activity, CheckCircle2, XCircle, Clock,
  TrendingUp, BarChart2, Radio, Settings, Trash2, Play,
  GitBranch, Package, Link2, HeartHandshake,
} from 'lucide-react'

type Overview = any

function timeAgo(ts: number | null): string {
  if (!ts) return '—'
  const s = Math.floor(Date.now()/1000 - ts)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s/60)}m ago`
  if (s < 86400) return `${Math.floor(s/3600)}h ago`
  return `${Math.floor(s/86400)}d ago`
}
function StatMini({ label, value, sub, icon }: any) {
  return (
    <div className="bg-gray-800/40 border border-gray-700/60 rounded-xl p-4">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-gray-400">{icon} {label}</div>
      <div className="text-xl font-bold text-white mt-1">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1 truncate">{sub}</div>}
    </div>
  )
}

export default function BrainPage(){
  const [data,setData]=useState<Overview|null>(null)
  const [loading,setLoading]=useState(true)
  const [err,setErr]=useState<string|null>(null)
  const [q,setQ]=useState('BTC momentum')
  const [sym,setSym]=useState('BTC/USDT')
  const [hits,setHits]=useState<any[]>([])
  const [searching,setSearching]=useState(false)
  const [pulling,setPulling]=useState(false)
  const [settling,setSettling]=useState(false)
  const [graph,setGraph]=useState<any|null>(null)
  const [tradeFilter,setTradeFilter]=useState<{symbol:string; outcome:string}>({symbol:'', outcome:''})
  const [tradeLimit,setTradeLimit]=useState(25)
  const [viewerMode,setViewerMode]=useState<'app'|'demo'>('app')
  const [viewerSrc,setViewerSrc]=useState<string>('https://neuroglancer-demo.appspot.com/fafb.html#!gs://fafb-ffn1/main_ng.json')
  const [viewerLoading,setViewerLoading]=useState(false)
  const [viewerMeta,setViewerMeta]=useState<{episodes: number; trades: number} | null>(null)
  const [hemibrain,setHemibrain]=useState<any|null>(null)
  const [rois,setRois]=useState<any[]|null>(null)
  const [pipeline,setPipeline]=useState<any[]|null>(null)
  const [neuprintQuery,setNeuprintQuery]=useState('MATCH (n:Neuron)-[e:ConnectsTo]->(m) RETURN n,m LIMIT 5')
  const [neuprintRes,setNeuprintRes]=useState<any|null>(null)
  const [neuprintSearching,setNeuprintSearching]=useState(false)
  const load = useCallback(async()=>{
    setLoading(true)
    try{
      const r = await apiClient.neuro.overview()
      setData(r.data); setErr(null)
      try{ const g = await apiClient.neuro.graph(80); setGraph(g.data)}catch{}
      try{ const hb = await apiClient.neuro.hemibrain(); setHemibrain(hb.data)}catch{}
      try{ const rs = await apiClient.neuro.rois(); setRois(rs.data.rois || rs.data.rois)}catch{}
      try{ const pl = await apiClient.neuro.pipeline(); setPipeline(pl.data.pipeline)}catch{}
    }catch(e:any){ setErr(e?.response?.data?.detail || e.message || 'Failed to load Brain overview')}
    finally{ setLoading(false)}
  },[])
  useEffect(()=>{ load() },[load])
  const runNeuprint = async()=>{
    if(!neuprintQuery.trim()) return
    setNeuprintSearching(true)
    try{ const r = await apiClient.neuro.neuprint(neuprintQuery.trim(), 25); setNeuprintRes(r.data)}catch(e:any){ setNeuprintRes({error: e?.response?.data?.detail || e.message})}
    finally{ setNeuprintSearching(false)}
  }
  // Load Neuroglancer viewer state from APP DATA (not demo) — encode into hash
  const loadViewer = useCallback(async(mode:'app'|'demo')=>{
    if(mode==='demo'){
      setViewerSrc('https://neuroglancer-demo.appspot.com/fafb.html#!gs://fafb-ffn1/main_ng.json')
      setViewerMeta(null)
      return
    }
    setViewerLoading(true)
    try{
      const r = await apiClient.neuro.viewerState(120, false)
      const state = r.data
      // Encode full state into the hash — avoids mixed-content fetch to http://localhost
      // and guarantees the viewer shows the app's *real* episodes + trade segments.
      const encoded = encodeURIComponent(JSON.stringify(state))
      // Use https demo host with encoded state (no fetch to localhost needed)
      setViewerSrc(`https://neuroglancer-demo.appspot.com/#!${encoded}`)
      const brainCount = (state.layers?.find((l:any)=>l.name==='brain-episodes')?.annotations?.length) || 0
      const tradeCount = (state.layers?.find((l:any)=>l.name==='trade-segments')?.annotations?.length) || 0
      setViewerMeta({episodes: brainCount, trades: tradeCount})
    }catch(e:any){
      // Fallback to demo if app viewer fails
      setViewerSrc('https://neuroglancer-demo.appspot.com/fafb.html#!gs://fafb-ffn1/main_ng.json')
    }finally{ setViewerLoading(false)}
  },[])
  useEffect(()=>{ loadViewer(viewerMode) },[viewerMode, loadViewer])
  // Keep app viewer fresh when overview loads (new episodes arrive via auto-sync)
  useEffect(()=>{ if(data && viewerMode==='app') loadViewer('app') },[data?.stats?.episodes_total, data?.stats?.segments_total])
  const runSearch=async()=>{
    if(!q.trim()) return
    setSearching(true)
    try{
      const r = await apiClient.neuro.search(q.trim(), sym.trim()||undefined, 12)
      setHits(r.data?.hits||[])
    }catch(e:any){ setErr(e?.response?.data?.detail||'Search failed')}
    finally{ setSearching(false)}
  }
  const pullRepo=async()=>{
    setPulling(true)
    try{ await apiClient.neuro.repoPull(); await load()}catch(e:any){ setErr(e?.response?.data?.detail||'Pull failed')}
    finally{ setPulling(false)}
  }
  const settle=async()=>{
    setSettling(true)
    try{ await apiClient.neuro.settle(12); await load()}catch(e:any){ setErr(e?.response?.data?.detail||'Settle failed')}
    finally{ setSettling(false)}
  }
  const filteredTrades = (() => {
    const segs = data?.trades?.recent || []
    // use overview.trades.recent as preview; full list via /neuro/trades
    return segs
  })()

  return (
    <>
      <Head><title>Brain | TradeBot — Neuroglancer Core</title></Head>
      <div className="max-w-6xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 via-violet-600 to-fuchsia-600 flex items-center justify-center shadow-lg shadow-violet-600/30">
              <Brain className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-white flex items-center gap-2">
                Core Brain
                <span className="text-[11px] font-normal px-2 py-0.5 rounded-full bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">google/neuroglancer</span>
                <span className="text-[10px] font-normal px-2 py-0.5 rounded-full bg-fuchsia-500/15 text-fuchsia-300 border border-fuchsia-500/30">CORE</span>
              </h1>
              <p className="text-sm text-gray-400">Neuroglancer volumetric brain — federates Hermes · Obsidian · AiAnalyst · Decisions · Journal into one recall surface. Linked to every agent + JARVIS/Paul + Trading Room.</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className={`px-3 py-1.5 rounded-lg text-xs font-semibold border ${data?.enabled ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : 'bg-red-500/15 text-red-300 border-red-500/30'}`}>
              {data?.enabled ? '● Neuro enabled' : '● Neuro disabled'}
            </span>
            <button onClick={load} className="p-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300"><RefreshCw className={`w-4 h-4 ${loading?'animate-spin':''}`} /></button>
          </div>
        </div>

        {err && <div className="flex items-center gap-2 px-4 py-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-300 text-sm"><XCircle className="w-4 h-4"/> {err}</div>}

        {loading && !data ? <div className="flex items-center gap-2 text-gray-400 text-sm"><RefreshCw className="w-4 h-4 animate-spin"/> Loading core brain…</div> : data && (
          <>
            {/* Repo banner */}
            <div className="rounded-xl border bg-gray-800/40 border-gray-700/60 p-4 flex flex-wrap items-center gap-4">
              <div className="flex items-center gap-2">
                <div className={`w-2 h-2 rounded-full ${data.repo.cloned ? 'bg-emerald-400 animate-pulse' : 'bg-red-400'}`} />
                <span className="text-sm font-semibold text-white">neuroglancer repo</span>
                {data.repo.pkg_version && <span className="text-xs px-2 py-0.5 rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">v{data.repo.pkg_version}</span>}
                {data.repo.commit && <span className="text-xs font-mono px-2 py-0.5 rounded bg-gray-900 border border-gray-700 text-gray-300">{data.repo.commit}</span>}
                {data.repo.branch && <span className="text-xs px-2 py-0.5 rounded bg-gray-700 text-gray-300">{data.repo.branch}</span>}
                {data.repo.dirty && <span className="text-xs px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">dirty</span>}
              </div>
              <div className="flex-1 min-w-0 text-xs text-gray-500 font-mono truncate">{data.repo.path} · {data.repo.remote}</div>
              <div className="flex items-center gap-2 text-xs text-gray-400 flex-wrap">
                {data.repo.commit_date && <span>{new Date(data.repo.commit_date).toLocaleString()}</span>}
                {data.repo.last_pull && <span className="px-2 py-0.5 rounded bg-gray-900 border border-gray-700">last pull {timeAgo(data.repo.last_pull ? new Date(data.repo.last_pull).getTime()/1000 : null)}</span>}
                <button onClick={pullRepo} disabled={pulling} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white text-xs font-medium">
                  {pulling ? <RefreshCw className="w-3.5 h-3.5 animate-spin"/> : <RefreshCw className="w-3.5 h-3.5"/> } Pull updates
                </button>
                <a href="https://github.com/google/neuroglancer" target="_blank" rel="noreferrer" className="flex items-center gap-1 px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 hover:text-white"><ExternalLink className="w-3 h-3"/> GitHub</a>
              </div>
              {!data.repo.cloned && <span className="w-full text-xs text-amber-300">Repo not cloned yet — run <code className="px-1 py-0.5 bg-black/30 rounded">python3 start.py</code> to auto-clone to <code className="px-1 py-0.5 bg-black/30 rounded">integrations/neuroglancer</code></span>}
            </div>

            {/* Stat strip */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatMini icon={<Database className="w-3.5 h-3.5"/>} label="Neuro episodes" value={data.stats.episodes_total} sub={`${data.stats.episodes_24h} in 24h · ${data.stats.fts_kb} KB · ${data.stats.db_path}`} />
              <StatMini icon={<TrendingUp className="w-3.5 h-3.5"/>} label="Trade segments" value={data.stats.segments_total} sub={`${data.stats.segments_pending} pending · auto-analyse every ${data.config.trade_loop_s}s`} />
              <StatMini icon={<Users className="w-3.5 h-3.5"/>} label="Agents wired" value={`${data.agents.length}`} sub={`${data.agents.length} linked · federation ${data.federation?'on':'off'}`} />
              <StatMini icon={<Layers className="w-3.5 h-3.5"/>} label="Brains federated" value={`${data.features.length}`} sub={`${data.hermes.episodes_total} hermes · ${data.obsidian.notes} vault · ${data.db.ai_providers||0} providers`} />
              <StatMini icon={<Clock className="w-3.5 h-3.5"/>} label="Last ingest" value={timeAgo(data.stats.last_ts)} sub={`pull every ${Math.round(data.config.pull_interval_s/3600)}h · auto-pull ${data.config.auto_pull?'on':'off'}`} />
            </div>

            <div className={`rounded-xl border p-3 flex items-center gap-2 text-sm ${data.enabled ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-200' : 'bg-gray-800/40 border-gray-700 text-gray-400'}`}>
              <Radio className="w-4 h-4"/>
              {data.enabled
                ? <>Neuro <strong>CORE brain</strong> is enabled — every Trading Room session + JARVIS turn is mirrored into neuro, and every agent prompt carries federated recall from Hermes + Vault + AiAnalyst. Disable via <code className="px-1 py-0.5 bg-black/30 rounded">NEUROGLANCER_ENABLED=false</code> in <code className="px-1 py-0.5 bg-black/30 rounded">.env</code>.</>
                : <>Neuro is <strong>disabled</strong> — federation off, no ingest. Enable with <code className="px-1 py-0.5 bg-black/30 rounded">NEUROGLANCER_ENABLED=true</code>.</>}
            </div>
            {/* Deciding brain + auto-sync status */}
            <div className={`rounded-xl border p-3 flex flex-wrap items-center gap-3 text-xs ${data.enabled ? 'bg-violet-500/10 border-violet-500/30 text-violet-200' : 'bg-gray-800/40 border-gray-700 text-gray-500'}`}>
              <span className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-bold border ${data.deciding ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : 'bg-amber-500/15 text-amber-300 border-amber-500/30'}`}>
                <Shield className="w-3.5 h-3.5" /> {data.deciding ? 'DECIDING BRAIN: ON' : 'DECIDING BRAIN: OFF'} — {data.deciding ? 'every BUY/SELL passes through Neuro veto' : 'Neuro only advises, no veto'}
              </span>
              <span className="flex items-center gap-1.5">
                <Activity className="w-3.5 h-3.5" /> Auto-sync
                <span className={`px-2 py-0.5 rounded-full border text-[11px] ${data.loops?.sync_loop?.running ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : 'bg-gray-700 text-gray-400 border-gray-600'}`}>
                  {data.loops?.sync_loop?.running ? `running every ${data.config.sync_interval_s || 300}s` : 'starting…'}
                </span>
                <button onClick={async()=>{ try{ const r=await apiClient.neuro.sync(30); if(r.data?.ok) { setErr(null); load(); } }catch(e:any){ setErr(e?.response?.data?.detail||'Sync failed')}}} className="ml-2 px-2.5 py-1 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-xs font-medium">Sync now</button>
              </span>
              <span className="ml-auto text-gray-400">Trade loops: {data.loops?.trade_loop?.running ? '●' : '○'} trade · {data.loops?.pull_loop?.running ? '●' : '○'} pull</span>
            </div>

            {/* Brains linked */}
            <div>
              <h2 className="text-sm font-semibold text-white flex items-center gap-2 mb-3"><Network className="w-4 h-4 text-cyan-400"/> Brains Linked to Core <span className="text-xs font-normal text-gray-500">({data.features.length} · federated recall = one query → every brain)</span></h2>
              <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
                {data.features.map((f:any)=>(
                  <div key={f.id} className={`rounded-xl border p-4 ${f.enabled?'bg-gray-800/40 border-gray-700/60':'bg-gray-900/40 border-gray-800 opacity-70'}`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold text-white">{f.label}</span>
                      <span className={`text-[11px] px-2 py-1 rounded-full font-semibold border ${f.enabled ? (f.status==='active' ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30' : 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30') : 'bg-gray-800 text-gray-500 border-gray-700'}`}>{f.enabled?f.status:'disabled'}</span>
                    </div>
                    <p className="text-xs text-gray-400 mt-1">{f.desc}</p>
                    <p className="text-xs text-gray-500 mt-1 font-mono truncate">{f.detail}</p>
                  </div>
                ))}
              </div>
            </div>

            {/* Agents wired */}
            <div>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-white flex items-center gap-2"><Users className="w-4 h-4 text-violet-400"/> Agents Wired to Core Brain <span className="text-xs font-normal text-gray-500">({data.agents.length} seats + JARVIS chair · PAUL/SOX same SOUL)</span></h2>
                <Link href="/trading-room" className="text-xs text-cyan-400 hover:text-cyan-300 flex items-center gap-1">Trading Room <ExternalLink className="w-3 h-3"/></Link>
              </div>
              <div className="grid md:grid-cols-3 gap-3">
                {data.agents.map((a:any)=>(
                  <div key={a.role} className={`rounded-xl border p-4 flex gap-3 ${a.connected?'bg-gray-800/40 border-gray-700/60':'bg-gray-900/30 border-gray-800'}`}>
                    <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0 border" style={{ background: `${a.color}18`, borderColor: `${a.color}40`, color: a.color }}>
                      <Brain className="w-5 h-5"/>
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-semibold text-white">{a.human_name}</span>
                        <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-300">{a.title}</span>
                        <span className={`w-2 h-2 rounded-full ${a.connected?'bg-emerald-400 animate-pulse':'bg-gray-600'}`} title={a.connected?'wired to neuro core':'not wired'} />
                      </div>
                      <div className="text-xs text-gray-500 truncate">{a.role} · seat {a.seat}</div>
                      <div className="text-xs text-gray-500">CORE braid · federated recall injected into every prompt</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Federated search + Trades side-by-side */}
            <div className="grid lg:grid-cols-2 gap-4">
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Search className="w-4 h-4 text-cyan-400"/> Federated Brains Search</h3>
                <p className="text-xs text-gray-500 mt-1">One query → Hermes + Obsidian + AiAnalyst + Paul + Decisions + Neuro local.</p>
                <div className="flex gap-2 mt-3">
                  <input value={q} onChange={e=>setQ(e.target.value)} placeholder="query (min 2 chars)" className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white" />
                  <input value={sym} onChange={e=>setSym(e.target.value)} placeholder="BTC/USDT" className="w-28 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white" />
                </div>
                <button onClick={runSearch} disabled={searching || !q.trim()} className="mt-2 w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white text-sm font-medium">
                  {searching ? <RefreshCw className="w-4 h-4 animate-spin"/> : <Search className="w-4 h-4"/>} Search federated
                </button>
                <div className="mt-3 space-y-2 max-h-72 overflow-auto">
                  {hits.length===0 && <div className="text-xs text-gray-600">{searching?'Searching…':'No hits yet — ingest a session or record a trade first.'}</div>}
                  {hits.map((h:any,i:number)=>(
                    <div key={i} className="rounded-lg bg-gray-900/60 border border-gray-700/40 p-2">
                      <div className="text-[11px] text-gray-500">{h.source || h.federated_source || h.kind} · {h.symbol || '-'} · {h.ts ? new Date(Number(h.ts)*1000).toLocaleString() : ''}</div>
                      <div className="text-xs text-gray-200 mt-1 line-clamp-3">{h.content}</div>
                    </div>
                  ))}
                </div>
                <div className="mt-3 flex gap-2 text-xs">
                  <Link href="/hermes" className="text-violet-400 hover:text-violet-300 flex items-center gap-1"><Atom className="w-3 h-3"/> Hermes</Link>
                  <Link href="/intelligence" className="text-cyan-400 hover:text-cyan-300 flex items-center gap-1"><Network className="w-3 h-3"/> Intelligence</Link>
                  <Link href="/vault" className="text-emerald-400 hover:text-emerald-300 flex items-center gap-1"><BookOpen className="w-3 h-3"/> Vault</Link>
                  <span className="ml-auto text-gray-600">federation {data.federation?'ON':'OFF'}</span>
                </div>
              </div>

              {/* Trades — records all pairs */}
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4 flex flex-col">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-white flex items-center gap-2"><BarChart2 className="w-4 h-4 text-amber-400"/> Trade Recording — All Pairs</h3>
                  <button onClick={settle} disabled={settling} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-medium">
                    {settling ? <RefreshCw className="w-3.5 h-3.5 animate-spin"/> : <Play className="w-3.5 h-3.5"/>} Settle now
                  </button>
                </div>
                <p className="text-xs text-gray-500 mt-1">Neuro records every pair (crypto + FX + metals + indices) via <code className="px-1 py-0.5 bg-gray-900 rounded">POST /neuro/trades/record</code> — Trading Room + JARVIS auto-ingest; setter auto-scans live candles for SL/TP.</p>
                <div className="grid grid-cols-4 gap-2 mt-3">
                  <div className="bg-gray-900/60 border border-gray-700/40 rounded-lg p-2 text-center">
                    <div className="text-[11px] text-gray-500">Total</div><div className="text-lg font-bold text-white">{data.trades.total}</div>
                  </div>
                  <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-2 text-center">
                    <div className="text-[11px] text-emerald-400">Wins</div><div className="text-lg font-bold text-emerald-300">{data.trades.wins}</div>
                  </div>
                  <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-2 text-center">
                    <div className="text-[11px] text-red-400">Losses</div><div className="text-lg font-bold text-red-300">{data.trades.losses}</div>
                  </div>
                  <div className="bg-gray-800 border border-gray-700/40 rounded-lg p-2 text-center">
                    <div className="text-[11px] text-gray-400">Win rate</div><div className="text-lg font-bold text-white">{data.trades.win_rate != null ? `${(data.trades.win_rate*100).toFixed(0)}%` : '—'}</div>
                  </div>
                </div>
                <div className="flex items-center gap-2 mt-3 flex-wrap">
                  {Object.entries(data.trades.by_asset_class||{}).slice(0,4).map(([k,v]:any)=>(
                    <span key={k} className="text-[11px] px-2 py-1 rounded-full bg-gray-800 border border-gray-700 text-gray-300">{k}: {v as number}</span>
                  ))}
                  {Object.keys(data.trades.by_asset_class||{}).length===0 && <span className="text-xs text-gray-600">No segments yet — record your first via the button below or auto via Trading Room signals.</span>}
                  <span className="ml-auto text-[11px] text-gray-500">{data.trades.pending} pending</span>
                </div>
                {/* top pairs */}
                {Object.keys(data.trades.by_symbol||{}).length>0 && (
                  <div className="flex flex-wrap gap-1.5 mt-2">
                    {Object.entries(data.trades.by_symbol).slice(0,6).map(([sym, st]:any)=>(
                      <span key={sym} className="text-[11px] px-2 py-1 rounded bg-gray-900 border border-gray-700 text-gray-300 font-mono">{sym} {st.wins}-{st.losses}{st.pending?` · ${st.pending}…`:''}</span>
                    ))}
                  </div>
                )}
                {/* recent table */}
                <div className="mt-3 max-h-64 overflow-auto border border-gray-700/40 rounded-lg divide-y divide-gray-700/40">
                  {(data.trades.recent||[]).length===0 && <div className="p-3 text-xs text-gray-600">No recent segments</div>}
                  {(data.trades.recent||[]).map((s:any)=>(
                    <div key={s.id} className="flex items-center gap-2 px-3 py-2 text-xs">
                      <span className={`w-2 h-2 rounded-full shrink-0 ${s.outcome==='win'?'bg-emerald-400': s.outcome==='loss'?'bg-red-400':'bg-gray-500'}`}/>
                      <span className="font-mono font-semibold text-white truncate">{s.symbol}</span>
                      <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${s.side==='long'?'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30':'bg-red-500/15 text-red-300 border border-red-500/30'}`}>{s.side}</span>
                      <span className="text-gray-400 truncate">{Number(s.entry).toFixed(2)} → TP {s.take_profit!=null?Number(s.take_profit).toFixed(2):'—'} · SL {s.stop_loss!=null?Number(s.stop_loss).toFixed(2):'—'}</span>
                      <span className="ml-auto text-gray-500 truncate">{s.timeframe || ''} {s.outcome || 'pending'}</span>
                    </div>
                  ))}
                </div>
                <div className="flex gap-2 mt-3">
                  <button onClick={async()=>{
                    const symbol=prompt('Symbol (e.g. BTC/USDT, XAUUSD, EURUSD)')?.trim()
                    if(!symbol) return
                    const side=prompt('Side long/short')?.trim()||'long'
                    const entry=Number(prompt('Entry price')||'')
                    const sl=Number(prompt('Stop loss')||'')
                    const tp=Number(prompt('Take profit')||'')
                    if(!entry||!sl||!tp) return alert('entry/sl/tp required')
                    try{
                      await apiClient.neuro.recordTrade({symbol, side, entry, stop_loss:sl, take_profit:tp, timeframe:'1h', source:'brain-manual'})
                      await load()
                    }catch(e:any){ setErr(e?.response?.data?.detail||'Record failed')}
                  }} className="px-3 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-medium">+ Record trade (any pair)</button>
                  <Link href="/history" className="ml-auto text-xs text-gray-400 hover:text-white flex items-center gap-1">Full history <ExternalLink className="w-3 h-3"/></Link>
                </div>
              </div>
            </div>

            {/* Graph */}
            <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><GitBranch className="w-4 h-4 text-violet-400"/> Brain Graph — Core links + Neuro viewer</h3>
                <a href="https://neuroglancer-docs.web.app/gallery/index.html#fafb-ffn1-full-adult-fly-brain-automated-segmentation" target="_blank" rel="noreferrer" className="text-xs text-gray-400 hover:text-white flex items-center gap-1">Gallery example ↗ <ExternalLink className="w-3 h-3"/></a>
              </div>
              <p className="text-xs text-gray-500 mt-1">Federation mesh: core ↔ each brain ↔ trade segments. Below is the live Neuroglancer volumetric viewer — the FAFB-FFN1 Full Adult Fly Brain automated segmentation gallery example, now embedded as the brain's visual surface. Trade signals map to segments; federation hits map to annotations.</p>
              {graph ? (
                <div className="mt-3 grid md:grid-cols-3 gap-3 text-xs">
                  <div className="bg-gray-900/60 border border-gray-700/40 rounded-lg p-3">
                    <div className="font-semibold text-white mb-2">Nodes ({graph.counts?.nodes||graph.nodes?.length||0})</div>
                    <div className="flex flex-wrap gap-1.5">
                      {(graph.nodes||[]).slice(0,18).map((n:any)=>(
                        <span key={n.id} className="px-2 py-1 rounded-full bg-gray-800 border border-gray-700 text-gray-300 text-[11px]">{n.label}</span>
                      ))}
                    </div>
                  </div>
                  <div className="bg-gray-900/60 border border-gray-700/40 rounded-lg p-3">
                    <div className="font-semibold text-white mb-2">Links ({graph.counts?.edges||graph.edges?.length||0})</div>
                    <div className="space-y-1 font-mono text-[11px] text-gray-500 max-h-40 overflow-auto">
                      {(graph.edges||[]).slice(0,12).map((e:any,i:number)=><div key={i}>{e.source} → {e.target} <span className="text-gray-600">({e.value})</span></div>)}
                    </div>
                  </div>
                  <div className="bg-gray-900/60 border border-gray-700/40 rounded-lg p-3">
                    <div className="font-semibold text-white mb-2">Gallery reference</div>
                    <div className="text-gray-400">This viewer loads <code className="px-1 py-0.5 bg-black/30 rounded">gs://fafb-ffn1/main_ng.json</code> — the FAFB Adult Fly Brain as published in the Neuroglancer gallery. Local build at <code className="px-1 py-0.5 bg-black/30 rounded">integrations/neuroglancer</code> can also be served at <code className="px-1 py-0.5 bg-black/30 rounded">/neuro-viewer</code>.</div>
                    <div className="mt-3 flex gap-2">
                      <Link href="/intelligence" className="px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 text-white text-xs font-medium">Intelligence 2D/3D</Link>
                      <Link href="/hermes" className="px-3 py-1.5 rounded-lg bg-gray-800 border border-gray-700 text-gray-300 text-xs">Hermes detail</Link>
                    </div>
                  </div>
                </div>
              ) : <div className="text-xs text-gray-600 mt-3">Graph loading…</div>}
              {/* Embedded Neuroglancer Viewer — APP DATA vs DEMO */}
              <div className="mt-4 rounded-xl overflow-hidden border border-cyan-500/30 bg-black">
                <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 bg-gray-900/80 border-b border-gray-700/50">
                  <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${viewerMode==='app' ? 'bg-emerald-400 animate-pulse' : 'bg-cyan-400 animate-pulse'}`} />
                    <span className="text-xs font-semibold text-white tracking-wide">
                      {viewerMode==='app' ? 'NEUROGLANCER — YOUR BRAIN (Live App Data)' : 'NEUROGLANCER — FAFB-FFN1 Full Adult Fly Brain (Demo)'}
                    </span>
                    {viewerMode==='app' && viewerMeta && <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">{viewerMeta.episodes} episodes · {viewerMeta.trades} trades</span>}
                    {viewerMode==='demo' && <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">4×4×40 nm</span>}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <div className="flex items-center rounded-full bg-gray-800 border border-gray-700 p-0.5">
                      <button onClick={()=>setViewerMode('app')} className={`px-2.5 py-1 rounded-full text-[11px] font-medium ${viewerMode==='app' ? 'bg-emerald-600 text-white' : 'text-gray-400 hover:text-white'}`}>App data</button>
                      <button onClick={()=>setViewerMode('demo')} className={`px-2.5 py-1 rounded-full text-[11px] font-medium ${viewerMode==='demo' ? 'bg-cyan-600 text-white' : 'text-gray-400 hover:text-white'}`}>FAFB demo</button>
                    </div>
                    {viewerMode==='app' && <button onClick={()=>loadViewer('app')} disabled={viewerLoading} className="text-[11px] px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 hover:text-white disabled:opacity-50 flex items-center gap-1"><RefreshCw className={`w-3 h-3 ${viewerLoading?'animate-spin':''}`}/> Refresh</button>}
                    <a href="https://neuroglancer-docs.web.app/gallery/index.html#fafb-ffn1-full-adult-fly-brain-automated-segmentation" target="_blank" rel="noreferrer" className="text-[11px] px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 hover:text-white flex items-center gap-1">Gallery <ExternalLink className="w-3 h-3"/></a>
                    <a href={viewerMode==='app' ? viewerSrc : 'https://neuroglancer-demo.appspot.com/fafb.html#!gs://fafb-ffn1/main_ng.json'} target="_blank" rel="noreferrer" className="text-[11px] px-2 py-1 rounded bg-cyan-600 hover:bg-cyan-500 text-white flex items-center gap-1">Open full viewer ↗</a>
                  </div>
                </div>
                {viewerMode==='app' ? (
                  <>
                    <iframe
                      key={viewerSrc}
                      title="Neuroglancer App Brain"
                      src={viewerSrc}
                      className="w-full"
                      style={{ height: '560px', border: 0, background: '#000' }}
                      allow="fullscreen"
                      loading="lazy"
                      referrerPolicy="no-referrer"
                    />
                    <div className="px-3 py-2 bg-gray-900/60 text-[11px] text-gray-500 flex flex-wrap gap-3">
                      <span>Source: <code className="px-1 py-0.5 bg-black/40 rounded">local://annotations</code> from <code className="px-1 py-0.5 bg-black/40 rounded">GET /api/v1/neuro/viewer-state</code> — your {viewerMeta?.episodes ?? data.stats.episodes_total} episodes + {viewerMeta?.trades ?? data.stats.segments_total} trades as 3D points (x=time, y=symbol, z=kind/confidence)</span>
                      <span className="ml-auto">Switch to <button onClick={()=>setViewerMode('demo')} className="text-cyan-400 hover:text-cyan-300 underline">FAFB demo</button> for the reference fly brain · <code className="px-1 py-0.5 bg-black/40 rounded">integrations/neuroglancer</code> v{data.repo.pkg_version || '2.41.2'}</span>
                    </div>
                  </>
                ) : (
                  <>
                    <iframe
                      title="Neuroglancer FAFB Fly Brain"
                      src="https://neuroglancer-demo.appspot.com/fafb.html#!gs://fafb-ffn1/main_ng.json"
                      className="w-full"
                      style={{ height: '560px', border: 0, background: '#000' }}
                      allow="fullscreen"
                      loading="lazy"
                      referrerPolicy="no-referrer"
                    />
                    <div className="px-3 py-2 bg-gray-900/60 text-[11px] text-gray-500 flex flex-wrap gap-3">
                      <span>Source: <code className="px-1 py-0.5 bg-black/40 rounded">precomputed://gs://fafb-ffn1/*</code> via <code className="px-1 py-0.5 bg-black/40 rounded">neuroglancer-demo.appspot.com/fafb.html</code></span>
                      <span className="ml-auto"><button onClick={()=>setViewerMode('app')} className="text-emerald-400 hover:text-emerald-300 underline">← Back to your app data</button> · <a href="https://neuroglancer-docs.web.app/gallery/index.html#fafb-ffn1-full-adult-fly-brain-automated-segmentation" target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-300 underline">fafb-ffn1-full-adult-fly-brain-automated-segmentation</a></span>
                    </div>
                  </>
                )}
              </div>
            </div>

            {/* Hemibrain — faithful to https://www.janelia.org/project-team/flyem/hemibrain */}
            <div className="bg-gradient-to-br from-gray-900/80 via-cyan-900/10 to-violet-900/10 border border-cyan-500/20 rounded-xl p-5 space-y-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-sm font-bold text-white flex items-center gap-2"><Atom className="w-4 h-4 text-cyan-400"/> FlyEM / Hemibrain — as Janelia describes it</h2>
                  <p className="text-xs text-gray-400 mt-1 max-w-3xl">
                    The hemibrain connectome is the largest synaptic-level connectome ever reconstructed — ~25,000 neurons grouped into thousands of cell types spanning mushroom body (MB), central complex (CX), antennal lobe, and downstream visual ROIs. It covers associative learning, navigation, and clock circuitry at 8 nm isotropic FIB-SEM. This Core Brain mirrors it exactly: <strong className="text-gray-200">MB→learning/memory, CX→navigation/risk, AL→sensory/news, OL-entry→vision/charts, clock→scheduler</strong>.
                  </p>
                </div>
                <a href="https://www.janelia.org/project-team/flyem/hemibrain" target="_blank" rel="noreferrer" className="shrink-0 text-[11px] px-2.5 py-1 rounded-full bg-cyan-600 hover:bg-cyan-500 text-white flex items-center gap-1">Janelia page ↗ <ExternalLink className="w-3 h-3"/></a>
              </div>
              {hemibrain && (
                <div className="grid md:grid-cols-3 gap-3 text-xs">
                  <div className="bg-black/30 border border-gray-700/40 rounded-lg p-3">
                    <div className="font-semibold text-cyan-300 mb-1">Getting started</div>
                    <ul className="space-y-1 text-gray-400 list-disc list-inside">
                      <li><a href={hemibrain.spec.paper} target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-300 underline">Overview paper — eLife 57443 (Scheffer et al. 2020)</a></li>
                      <li><a href={hemibrain.spec.biorxiv} target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-300 underline">BioRxiv preprint</a></li>
                      <li>Licensed <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noreferrer" className="text-emerald-400">CC-BY 4.0</a> — {hemibrain.spec.neurons.toLocaleString()} neurons, {hemibrain.spec.version}</li>
                      <li><a href={hemibrain.spec.neuprint_url} target="_blank" rel="noreferrer" className="text-violet-400 hover:text-violet-300 underline">Explore in neuPrint</a> · <a href={hemibrain.spec.neuroglancer_demo} target="_blank" rel="noreferrer" className="text-cyan-400 hover:text-cyan-300 underline">View in neuroglancer</a> (also below)</li>
                    </ul>
                    <div className="mt-2 flex gap-1.5 flex-wrap">
                      <a href={hemibrain.spec.neuroglancer_demo} target="_blank" rel="noreferrer" className="px-2 py-1 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-[11px]">Open Hemibrain viewer</a>
                      <button onClick={()=>apiClient.neuro.adjacencies(100,'csv').then(r=>{ const url=URL.createObjectURL(new Blob([r.data],{type:'text/csv'})); const a=document.createElement('a'); a.href=url; a.download='tradebot-adjacencies.csv'; a.click()})} className="px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 hover:text-white text-[11px]">Download connection matrix CSV</button>
                    </div>
                  </div>
                  <div className="bg-black/30 border border-gray-700/40 rounded-lg p-3">
                    <div className="font-semibold text-violet-300 mb-1">Dataset</div>
                    <p className="text-gray-400">Mushroom body + central complex + clock + ~4000 types. ROI segmentation at <code className="px-1 py-0.5 bg-black/40 rounded">gs://flyem-views/hemibrain/v1.2/rois</code>. Our viewer loads that 8 nm volume plus your live annotations at the hemibrain center.</p>
                    <div className="mt-2 text-[11px] text-gray-500">{hemibrain.spec.neurons.toLocaleString()} neurons · thousands of types · {hemibrain.spec.version} · {hemibrain.spec.released}</div>
                    <a href="https://storage.cloud.google.com/hemibrain/v1.2/exported-traced-adjacencies-v1.2.tar.gz" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 mt-2 text-[11px] px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300">Janelia adjacency tar.gz ↗</a>
                  </div>
                  <div className="bg-black/30 border border-gray-700/40 rounded-lg p-3">
                    <div className="font-semibold text-amber-300 mb-1">Pipeline (Fig. 715×302)</div>
                    <img src="https://www.janelia.org/sites/default/files/Connectome-flow-chart-horizontal_715x302.png" alt="Connectome pipeline" className="w-full rounded border border-gray-700/40 bg-white p-1" />
                    <p className="text-[11px] text-gray-500 mt-1">Hot-knife → FIB-SEM → stitching → FFN segmentation (Google) → synapse prediction (T-bar/PSD) → DVID → neu3/neutu → neuPrint</p>
                  </div>
                </div>
              )}
              {/* ROIs */}
              <div>
                <h3 className="text-xs font-semibold text-white flex items-center gap-2"><Layers className="w-3.5 h-3.5 text-cyan-400"/> ROIs — compartments spanning several brain regions</h3>
                <p className="text-[11px] text-gray-500 mt-1">Hemibrain ROIs (as in v1.2 `rois` segmentation). Each maps to a TradeBot domain — filtering by ROI is like filtering by brain region.</p>
                <div className="mt-2 grid md:grid-cols-2 lg:grid-cols-3 gap-2 max-h-64 overflow-auto pr-1">
                  {(rois || hemibrain?.rois || []).slice(0,12).map((r:any)=>(
                    <div key={r.id} className="rounded-lg border p-2.5 flex gap-2" style={{background:`${r.color}10`, borderColor:`${r.color}30`}}>
                      <div className="w-2 h-2 rounded-full mt-1 shrink-0" style={{background:r.color}} />
                      <div className="min-w-0">
                        <div className="text-xs font-bold text-white">{r.id} — {r.name}</div>
                        <div className="text-[11px] text-gray-400">{r.hemibrain_role}</div>
                        <div className="text-[11px] text-cyan-300 truncate">↳ {r.tradebot_maps_to}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
              {/* Cell types */}
              {hemibrain?.cell_families && (
                <div>
                  <h3 className="text-xs font-semibold text-white flex items-center gap-2"><Sparkles className="w-3.5 h-3.5 text-violet-400"/> Cell types — thousands, grouped</h3>
                  <div className="mt-2 grid md:grid-cols-2 gap-2">
                    {hemibrain.cell_families.map((c:any)=>(
                      <div key={c.family} className="rounded-lg bg-gray-900/40 border border-gray-700/40 p-2.5">
                        <div className="text-xs font-semibold text-white">{c.family}</div>
                        <div className="text-[11px] text-gray-500">{c.hemibrain_desc}</div>
                        <div className="text-[11px] text-cyan-300">TradeBot: {c.tradebot_cells}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {/* Pipeline */}
              {pipeline && (
                <div>
                  <h3 className="text-xs font-semibold text-white flex items-center gap-2"><GitBranch className="w-3.5 h-3.5 text-amber-400"/> Reconstruction pipeline — TradeBot mirrors every stage</h3>
                  <div className="mt-2 overflow-x-auto">
                    <table className="w-full text-[11px] border-collapse">
                      <thead><tr className="text-gray-500 border-b border-gray-700/40"><th className="text-left py-1 pr-2">Stage</th><th className="text-left py-1 pr-2">Hemibrain (Janelia)</th><th className="text-left py-1">TradeBot</th></tr></thead>
                      <tbody>
                        {pipeline.map((p:any)=>(
                          <tr key={p.stage} className="border-b border-gray-800/50">
                            <td className="py-1.5 pr-2 font-medium text-white whitespace-nowrap">{p.stage}</td>
                            <td className="py-1.5 pr-2 text-gray-400">{p.hemibrain}</td>
                            <td className="py-1.5 text-cyan-300">{p.tradebot}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              {/* neuPrint */}
              <div className="bg-black/30 border border-violet-500/20 rounded-lg p-3">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-xs font-semibold text-white flex items-center gap-2"><Network className="w-3.5 h-3.5 text-violet-400"/> neuPrint — analysis ecosystem (like <a href="https://neuprint.janelia.org" target="_blank" rel="noreferrer" className="text-violet-400 underline">neuprint.janelia.org</a>)</h3>
                  <a href="https://neuprint.janelia.org/help" target="_blank" rel="noreferrer" className="text-[11px] px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300">Help ↗</a>
                </div>
                <p className="text-[11px] text-gray-500 mt-1">Query the connectome with Cypher — here the connectome is your federated brain (episodes→trades, ROI-aware). Try the Janelia examples; they are rewritten to our graph.</p>
                <div className="mt-2 flex gap-2">
                  <input value={neuprintQuery} onChange={e=>setNeuprintQuery(e.target.value)} placeholder="MATCH (n:Neuron)-[e:ConnectsTo]->(m) RETURN n,m LIMIT 5" className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-white font-mono" />
                  <button onClick={runNeuprint} disabled={neuprintSearching} className="px-3 py-1.5 rounded-lg bg-violet-600 hover:bg-violet-500 disabled:opacity-50 text-white text-xs font-medium">{neuprintSearching? '…':'Query'}</button>
                </div>
                {neuprintRes && (
                  <pre className="mt-2 bg-gray-950 border border-gray-800 rounded-lg p-2 text-[11px] text-gray-300 max-h-64 overflow-auto whitespace-pre-wrap">{JSON.stringify(neuprintRes, null, 2).slice(0,8000)}</pre>
                )}
                <div className="mt-2 flex gap-2 text-[11px]">
                  <a href="https://github.com/connectome-neuprint/neuprint-python" target="_blank" rel="noreferrer" className="text-gray-500 hover:text-gray-300">neuprint-python ↗</a>
                  <a href="https://storage.cloud.google.com/hemibrain/v1.2/exported-traced-adjacencies-v1.2.tar.gz" target="_blank" rel="noreferrer" className="text-gray-500 hover:text-gray-300">adjacencies tar.gz ↗</a>
                  <span className="ml-auto text-gray-600">Janelia: CC-BY 4.0 · 25k neurons</span>
                </div>
              </div>
            </div>

            {/* Config + quick actions */}
            <div className="grid lg:grid-cols-2 gap-4">
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><Cpu className="w-4 h-4 text-gray-400"/> Config + Loops</h3>
                <div className="mt-2 space-y-1 text-xs font-mono">
                  <div className="flex justify-between"><span className="text-gray-500">NEUROGLANCER_ENABLED</span><span className={data.enabled?'text-emerald-400':'text-red-400'}>{String(data.enabled)}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">NEUROGLANCER_FEDERATION_ENABLED</span><span className={String(data.federation)==='true'?'text-emerald-400':'text-gray-400'}>{String(data.federation)}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Repo branch</span><span className="text-gray-300">{data.config.branch}</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Pull interval</span><span className="text-gray-300">{Math.round(data.config.pull_interval_s/3600)}h</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">Trade settle interval</span><span className="text-gray-300">{Math.round(data.config.trade_loop_s/60)}m</span></div>
                  <div className="flex justify-between"><span className="text-gray-500">State path</span><span className="text-gray-300 truncate ml-2">{data.config.state_path}</span></div>
                </div>
                <div className="flex gap-2 mt-3">
                  <button onClick={async()=>{ try{ await apiClient.neuro.prune(90); await load()}catch{}}} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-xs text-white"><Trash2 className="w-3.5 h-3.5"/> Prune 90d</button>
                  <Link href="/trading-room-settings" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 border border-gray-700 text-xs text-gray-300 hover:text-white"><Settings className="w-3.5 h-3.5"/> Room settings</Link>
                  <Link href="/system-monitor" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 border border-gray-700 text-xs text-gray-300 hover:text-white"><Activity className="w-3.5 h-3.5"/> System</Link>
                </div>
              </div>
              <div className="bg-gray-800/30 border border-gray-700/60 rounded-xl p-4">
                <h3 className="text-sm font-semibold text-white flex items-center gap-2"><HeartHandshake className="w-4 h-4 text-fuchsia-400"/> Access — everyone sees the core</h3>
                <ul className="mt-2 space-y-1.5 text-xs text-gray-400 list-disc list-inside">
                  <li><strong className="text-gray-300">All agents (7 seats + JARVIS)</strong> receive federated neuro recall in every prompt (orchestrator + room worker).</li>
                  <li><strong className="text-gray-300">Hermes</strong> is mirrored into neuro; its skills feed the room via the same recall (no duplicate index).</li>
                  <li><strong className="text-gray-300">JARVIS / Paul / SOX</strong> chat is federated: PaulChat queries <code className="px-1 py-0.5 bg-gray-900 rounded">GET /neuro/search</code> alongside Hermes; brain managers write to neuro too.</li>
                  <li><strong className="text-gray-300">Trading Room + sniper + signals + MT5</strong> auto-ingest sessions → neuro; manual <code className="px-1 py-0.5 bg-gray-900 rounded">/neuro/trades/record</code> covers any pair.</li>
                </ul>
                <p className="text-[11px] text-gray-600 mt-3">Scoring stays on Postgres (<code className="px-1 py-0.5 bg-gray-900 rounded">AgentDecision</code>). Neuro FTS is recall-only + trade segmentation, 90-day prune matching Hermes.</p>
              </div>
            </div>
          </>
        )}
      </div>
    </>
  )
}

