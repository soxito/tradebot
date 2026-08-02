import Head from 'next/head'
import { useState, useEffect, useCallback } from 'react'
import { apiClient, getApiBaseUrl } from '@/services/api'
import { useZarRate } from '@/hooks/useZarRate'
import {
  RefreshCw, Brain, Newspaper, BarChart3, MessageSquare,
  TrendingUp, TrendingDown, Minus, ChevronDown, ChevronUp,
  ExternalLink, Filter, Clock, Database, Activity
} from 'lucide-react'

type Tab = 'overview' | 'news' | 'decisions' | 'sentiment' | 'learning'

interface NewsArticle {
  id: number
  title: string
  summary: string | null
  source: string
  url: string | null
  category: string | null
  symbols: string[]
  reliability: number
  sentiment_score: number | null
  sentiment_label: string | null
  published_at: string | null
  fetched_at: string
}

interface AgentDecision {
  id: number
  agent_name: string
  agent_role: string
  symbol: string
  action: string
  confidence: number
  reasoning: string
  session_id: string | null
  ai_called: boolean
  memory_context_used: number
  outcome: string | null
  outcome_pnl: number | null
  created_at: string
}

interface SentimentData {
  symbol: string
  score: number
  magnitude: number
  news_score?: number
  social_score?: number
  technical_score?: number
  sources_count: number
  created_at: string
}

export default function InsightsPage() {
  const { toZar } = useZarRate()
  const [tab, setTab] = useState<Tab>('overview')
  const [loading, setLoading] = useState(true)
  const [brainCapture, setBrainCapture] = useState<'idle' | 'capturing' | 'done'>('idle')

  // Data state
  const [articles, setArticles] = useState<NewsArticle[]>([])
  const [newsStats, setNewsStats] = useState<any>(null)
  const [decisions, setDecisions] = useState<AgentDecision[]>([])
  const [sentiments, setSentiments] = useState<SentimentData[]>([])
  const [enhancedSentiments, setEnhancedSentiments] = useState<any[]>([])
  const [learningStats, setLearningStats] = useState<any>(null)
  const [pipelineStatus, setPipelineStatus] = useState<any>(null)

  // Filters
  const [newsSource, setNewsSource] = useState('')
  const [newsSymbol, setNewsSymbol] = useState('')
  const [newsCategory, setNewsCategory] = useState('')
  const [newsHours, setNewsHours] = useState(24)
  const [decisionSymbol, setDecisionSymbol] = useState('')
  const [decisionRole, setDecisionRole] = useState('')
  const [decisionLimit, setDecisionLimit] = useState(100)
  const [expandedDecision, setExpandedDecision] = useState<number | null>(null)

  const fetchOverview = useCallback(async () => {
    setLoading(true)
    try {
      const [statsRes, sentRes, pipeRes, learnRes, decisionsRes, newsRes, paulKnRes] = await Promise.allSettled([
        apiClient.getNewsStats(),
        apiClient.getEnhancedSentiments(),
        apiClient.getPipelineStatus(),
        apiClient.getLearningStats(),
        apiClient.getAgentDecisions({ limit: 20 }),
        apiClient.getNewsArticles({ limit: 10 }),
        // @ts-ignore — paul knowledge stats
        fetch(`${getApiBaseUrl()}/plugins/agent-paul/knowledge/stats`).then(r => r.json()),
      ])
      if (statsRes.status === 'fulfilled') setNewsStats(statsRes.value.data)
      const sentData = sentRes.status === 'fulfilled' ? (sentRes.value.data?.sentiments || []) : []
      if (sentRes.status === 'fulfilled') setEnhancedSentiments(sentData)
      const pipeData = pipeRes.status === 'fulfilled' ? pipeRes.value.data : {}
      if (pipeRes.status === 'fulfilled') setPipelineStatus(pipeData)
      const learnData = learnRes.status === 'fulfilled' ? learnRes.value.data : {}
      if (learnRes.status === 'fulfilled') setLearningStats(learnData)
      const decsData = decisionsRes.status === 'fulfilled' ? (decisionsRes.value.data?.decisions || decisionsRes.value.data || []) : []
      if (decisionsRes.status === 'fulfilled') setDecisions(decsData)
      const newsData = newsRes.status === 'fulfilled' ? (newsRes.value.data?.articles || []) : []
      const paulKn = paulKnRes.status === 'fulfilled' ? paulKnRes.value : {}

      // ── Self-learning: capture full brain snapshot to Obsidian vault ─────
      // Fire-and-forget so it never blocks the overview render.
      setBrainCapture('capturing')
      apiClient.obsidian.insightsHarvest({
        decisions: decsData,
        news_articles: newsData,
        sentiments: sentData,
        learning_stats: learnData,
        pipeline_status: pipeData,
        paul_knowledge_stats: paulKn,
      }).then(() => {
        setBrainCapture('done')
        setTimeout(() => setBrainCapture('idle'), 4000)
      }).catch(() => setBrainCapture('idle'))

    } catch {} finally { setLoading(false) }
  }, [])

  const fetchNews = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiClient.getNewsArticles({
        source: newsSource || undefined,
        symbol: newsSymbol || undefined,
        category: newsCategory || undefined,
        hours: newsHours,
        limit: 200,
      })
      setArticles(res.data?.articles || [])
    } catch {} finally { setLoading(false) }
  }, [newsSource, newsSymbol, newsCategory, newsHours])

  const fetchDecisions = useCallback(async () => {
    setLoading(true)
    try {
      const params: any = { limit: decisionLimit }
      if (decisionSymbol) params.symbol = decisionSymbol
      const res = await apiClient.getAgentDecisions(params)
      setDecisions(res.data?.decisions || res.data || [])
    } catch {} finally { setLoading(false) }
  }, [decisionSymbol, decisionLimit])

  const fetchSentiment = useCallback(async () => {
    setLoading(true)
    try {
      const [basicRes, enhRes] = await Promise.allSettled([
        apiClient.getSentiments(),
        apiClient.getEnhancedSentiments(),
      ])
      if (basicRes.status === 'fulfilled') setSentiments(basicRes.value.data?.sentiments || [])
      if (enhRes.status === 'fulfilled') setEnhancedSentiments(enhRes.value.data?.sentiments || [])
    } catch {} finally { setLoading(false) }
  }, [])

  const fetchLearning = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiClient.getLearningStats({ role: decisionRole || undefined })
      const data = res.data
      setLearningStats(data)
      // Capture learning stats to vault for self-improvement
      if (data?.total_decisions > 0) {
        const summary = `AI learning update: ${data.total_decisions} decisions, ` +
          `${data.ai_calls} AI calls, ${data.local_pct?.toFixed(1)}% local, ` +
          `win rate ${data.win_rate?.toFixed(1)}%. Knowledge base actively learning.`
        apiClient.obsidian.jarvisLearn({
          question: `TradeBot learning stats — ${new Date().toLocaleDateString()}`,
          answer: summary,
          page: '/insights',
          tags: ['learning', 'stats', 'ai'],
        }).catch(() => {})
      }
    } catch {} finally { setLoading(false) }
  }, [decisionRole])

  useEffect(() => {
    if (tab === 'overview') fetchOverview()
    else if (tab === 'news') fetchNews()
    else if (tab === 'decisions') fetchDecisions()
    else if (tab === 'sentiment') fetchSentiment()
    else if (tab === 'learning') fetchLearning()
  }, [tab, fetchOverview, fetchNews, fetchDecisions, fetchSentiment, fetchLearning])

  const sentimentIcon = (score: number) => {
    if (score > 0.05) return <TrendingUp className="w-3.5 h-3.5 text-green-400" />
    if (score < -0.05) return <TrendingDown className="w-3.5 h-3.5 text-red-400" />
    return <Minus className="w-3.5 h-3.5 text-gray-500" />
  }
  const sentimentColor = (score: number) => score > 0.05 ? 'text-green-400' : score < -0.05 ? 'text-red-400' : 'text-gray-400'
  const sentimentLabel = (score: number) => score > 0.05 ? 'Bullish' : score < -0.05 ? 'Bearish' : 'Neutral'

  const actionColor = (action: string) => {
    const a = action.toLowerCase()
    if (['buy', 'approve', 'adjust'].includes(a)) return 'text-green-400 bg-green-500/10 border-green-500/30'
    if (['sell', 'reject', 'cancel', 'close'].includes(a)) return 'text-red-400 bg-red-500/10 border-red-500/30'
    return 'text-gray-400 bg-gray-500/10 border-gray-500/30'
  }

  const roleColor = (role: string) => {
    const r = role.toLowerCase()
    if (r.includes('market')) return 'text-blue-400'
    if (r.includes('signal')) return 'text-cyan-400'
    if (r.includes('risk')) return 'text-orange-400'
    if (r.includes('executor') || r.includes('trade')) return 'text-purple-400'
    if (r.includes('sentiment')) return 'text-pink-400'
    if (r.includes('position')) return 'text-yellow-400'
    return 'text-gray-400'
  }

  const timeAgo = (dateStr: string) => {
    const diff = Date.now() - new Date(dateStr).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1) return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return `${hrs}h ago`
    const days = Math.floor(hrs / 24)
    return `${days}d ago`
  }

  const tabs: { id: Tab; label: string; icon: any }[] = [
    { id: 'overview', label: 'Overview', icon: BarChart3 },
    { id: 'news', label: 'News & RSS', icon: Newspaper },
    { id: 'decisions', label: 'AI Decisions', icon: Brain },
    { id: 'sentiment', label: 'Sentiment', icon: Activity },
    { id: 'learning', label: 'AI Learning', icon: Database },
  ]

  return (
    <>
      <Head><title>TradeBot - Insights</title></Head>
      <div className="space-y-4 max-w-7xl mx-auto">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-white flex items-center gap-3">
              Insights & Data
              {/* ── Brain capture status badge ────────────────────────────── */}
              {brainCapture === 'capturing' && (
                <span className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-violet-900/30 border border-violet-500/40 text-violet-300 animate-pulse">
                  <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-ping" />
                  Capturing to brain…
                </span>
              )}
              {brainCapture === 'done' && (
                <span className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-emerald-900/30 border border-emerald-500/40 text-emerald-300">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
                  ✓ Brain updated
                </span>
              )}
            </h1>
            <p className="text-xs text-gray-500 mt-0.5 flex items-center gap-2">
              AI decisions, news feeds, sentiment data, and learning analytics
              {brainCapture !== 'idle' ? null : (
                <a href="/intelligence" className="text-violet-400 hover:text-violet-300">
                  → View in brain
                </a>
              )}
            </p>
          </div>
          <button
            onClick={() => {
              if (tab === 'overview') fetchOverview()
              else if (tab === 'news') fetchNews()
              else if (tab === 'decisions') fetchDecisions()
              else if (tab === 'sentiment') fetchSentiment()
              else if (tab === 'learning') fetchLearning()
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-gray-800 border border-gray-700 rounded-lg text-gray-300 hover:bg-gray-700 transition"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 bg-gray-900/50 border border-gray-800 rounded-lg p-1">
          {tabs.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium rounded-md transition flex-1 justify-center ${
                tab === t.id
                  ? 'bg-gray-700 text-white shadow-sm'
                  : 'text-gray-500 hover:text-gray-300 hover:bg-gray-800/50'
              }`}
            >
              <t.icon className="w-3.5 h-3.5" />
              {t.label}
            </button>
          ))}
        </div>

        {/* ═══════════════════ OVERVIEW TAB ═══════════════════ */}
        {tab === 'overview' && (
          <div className="space-y-4">
            {/* Stats Cards */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <div className="text-xs text-gray-500 mb-1">News Articles</div>
                <div className="text-2xl font-bold text-white">{newsStats?.total ?? '—'}</div>
                <div className="text-[10px] text-gray-600 mt-1">{newsStats?.sources ? Object.keys(newsStats.sources).length : 0} sources active</div>
              </div>
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <div className="text-xs text-gray-500 mb-1">AI Decisions</div>
                <div className="text-2xl font-bold text-white">{decisions.length > 0 ? `${decisions.length}+` : '—'}</div>
                <div className="text-[10px] text-gray-600 mt-1">Latest {decisions.length} loaded</div>
              </div>
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <div className="text-xs text-gray-500 mb-1">Sentiment Tracked</div>
                <div className="text-2xl font-bold text-white">{enhancedSentiments.length || '—'}</div>
                <div className="text-[10px] text-gray-600 mt-1">symbols monitored</div>
              </div>
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <div className="text-xs text-gray-500 mb-1">Pipeline Status</div>
                <div className="text-lg font-bold">
                  {pipelineStatus?.running ? (
                    <span className="text-green-400">Active</span>
                  ) : (
                    <span className="text-red-400">Stopped</span>
                  )}
                </div>
                <div className="text-[10px] text-gray-600 mt-1">
                  {pipelineStatus?.interval ? `Every ${pipelineStatus.interval}s` : 'Not running'}
                </div>
              </div>
            </div>

            {/* News Source Breakdown */}
            {newsStats?.sources && Object.keys(newsStats.sources).length > 0 && (
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                  <Newspaper className="w-4 h-4 text-blue-400" /> News Sources
                </h3>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2">
                  {Object.entries(newsStats.sources as Record<string, number>)
                    .sort(([, a], [, b]) => (b as number) - (a as number))
                    .map(([source, count]) => (
                      <div key={source} className="flex items-center justify-between bg-gray-900/50 rounded px-2.5 py-1.5">
                        <span className="text-[11px] text-gray-300 truncate">{source}</span>
                        <span className="text-[11px] text-blue-400 font-mono ml-2">{count as number}</span>
                      </div>
                    ))}
                </div>
              </div>
            )}

            {/* Category Breakdown */}
            {newsStats?.categories && Object.keys(newsStats.categories).length > 0 && (
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <h3 className="text-sm font-semibold text-white mb-3">Article Categories</h3>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(newsStats.categories as Record<string, number>)
                    .sort(([, a], [, b]) => (b as number) - (a as number))
                    .map(([cat, count]) => (
                      <span key={cat} className="px-2.5 py-1 rounded-full bg-gray-900/50 border border-gray-700 text-[11px] text-gray-300">
                        {cat} <span className="text-gray-500 font-mono ml-1">{count as number}</span>
                      </span>
                    ))}
                </div>
              </div>
            )}

            {/* Recent Sentiment Overview */}
            {enhancedSentiments.length > 0 && (
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                  <Activity className="w-4 h-4 text-purple-400" /> Sentiment Overview
                </h3>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2">
                  {enhancedSentiments.map((s: any) => {
                    const score = s.score ?? s.sentiment_score ?? 0
                    return (
                      <div key={s.symbol} className="bg-gray-900/50 rounded-lg p-2.5 border border-gray-800">
                        <div className="flex items-center justify-between mb-1">
                          <span className="font-mono font-bold text-sm text-white">{s.symbol}</span>
                          {sentimentIcon(score)}
                        </div>
                        <div className={`text-sm font-semibold ${sentimentColor(score)}`}>
                          {sentimentLabel(score)}
                        </div>
                        <div className="text-[10px] text-gray-500 font-mono">
                          {(score * 100).toFixed(1)}% • {s.sources_count ?? 0} sources
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}

            {/* Recent AI Decisions */}
            {decisions.length > 0 && (
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                  <Brain className="w-4 h-4 text-cyan-400" /> Recent AI Decisions
                </h3>
                <div className="space-y-1.5 max-h-[300px] overflow-y-auto">
                  {decisions.slice(0, 15).map(d => (
                    <div key={d.id} className="flex items-center gap-2 px-2.5 py-1.5 bg-gray-900/30 rounded text-[11px]">
                      <span className="text-gray-500 w-14 shrink-0">{timeAgo(d.created_at)}</span>
                      <span className={`font-medium w-24 truncate ${roleColor(d.agent_role)}`}>{d.agent_name}</span>
                      <span className="font-mono font-medium text-white w-20">{d.symbol}</span>
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold border ${actionColor(d.action)}`}>
                        {d.action.toUpperCase()}
                      </span>
                      <span className="text-gray-500 font-mono w-10">{(d.confidence * 100).toFixed(0)}%</span>
                      <span className="text-gray-600 truncate flex-1">{d.reasoning?.slice(0, 80)}</span>
                      {d.ai_called && <span className="text-purple-500 text-[9px]">AI</span>}
                      {!d.ai_called && <span className="text-yellow-600 text-[9px]">MEM</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Learning Stats */}
            {learningStats && (
              <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
                  <Database className="w-4 h-4 text-yellow-400" /> AI Learning Stats
                </h3>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  {learningStats.total_decisions != null && (
                    <div className="bg-gray-900/50 rounded p-2.5">
                      <div className="text-[10px] text-gray-500">Total Decisions</div>
                      <div className="text-lg font-bold text-white">{learningStats.total_decisions}</div>
                    </div>
                  )}
                  {learningStats.ai_calls != null && (
                    <div className="bg-gray-900/50 rounded p-2.5">
                      <div className="text-[10px] text-gray-500">AI API Calls</div>
                      <div className="text-lg font-bold text-purple-400">{learningStats.ai_calls}</div>
                    </div>
                  )}
                  {learningStats.memory_decisions != null && (
                    <div className="bg-gray-900/50 rounded p-2.5">
                      <div className="text-[10px] text-gray-500">Memory Decisions</div>
                      <div className="text-lg font-bold text-yellow-400">{learningStats.memory_decisions}</div>
                    </div>
                  )}
                  {learningStats.win_rate != null && (
                    <div className="bg-gray-900/50 rounded p-2.5">
                      <div className="text-[10px] text-gray-500">Win Rate</div>
                      <div className={`text-lg font-bold ${learningStats.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}`}>
                        {learningStats.win_rate.toFixed(1)}%
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ═══════════════════ NEWS TAB ═══════════════════ */}
        {tab === 'news' && (
          <div className="space-y-3">
            {/* Filters */}
            <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <Filter className="w-3.5 h-3.5 text-gray-500" />
                <input
                  type="text"
                  placeholder="Symbol (BTC, ETH...)"
                  value={newsSymbol}
                  onChange={e => setNewsSymbol(e.target.value)}
                  className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300 w-32"
                />
                <input
                  type="text"
                  placeholder="Source (reuters, coindesk...)"
                  value={newsSource}
                  onChange={e => setNewsSource(e.target.value)}
                  className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300 w-44"
                />
                <select
                  value={newsCategory}
                  onChange={e => setNewsCategory(e.target.value)}
                  className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300"
                >
                  <option value="">All Categories</option>
                  <option value="crypto">Crypto</option>
                  <option value="macro">Macro</option>
                  <option value="forex">Forex</option>
                  <option value="stocks">Stocks</option>
                  <option value="futures">Futures</option>
                </select>
                <select
                  value={newsHours}
                  onChange={e => setNewsHours(Number(e.target.value))}
                  className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300"
                >
                  <option value={6}>Last 6h</option>
                  <option value={12}>Last 12h</option>
                  <option value={24}>Last 24h</option>
                  <option value={48}>Last 48h</option>
                  <option value={168}>Last 7d</option>
                </select>
                <button
                  onClick={fetchNews}
                  className="px-3 py-1.5 bg-blue-600/20 border border-blue-500/30 text-blue-300 rounded text-xs hover:bg-blue-600/30 transition"
                >
                  Search
                </button>
                <span className="ml-auto text-[10px] text-gray-500">{articles.length} articles</span>
              </div>
            </div>

            {/* Articles List */}
            {loading ? (
              <div className="text-center py-8 text-gray-500 text-sm">Loading news...</div>
            ) : articles.length === 0 ? (
              <div className="text-center py-8 text-gray-500 text-sm">No articles found for this filter</div>
            ) : (
              <div className="space-y-1.5">
                {articles.map(a => (
                  <div key={a.id} className="bg-gray-800/30 border border-gray-800 rounded-lg p-3 hover:bg-gray-800/50 transition">
                    <div className="flex items-start gap-3">
                      {/* Sentiment indicator */}
                      <div className="pt-0.5 shrink-0">
                        {a.sentiment_score != null ? sentimentIcon(a.sentiment_score) : <Minus className="w-3.5 h-3.5 text-gray-700" />}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-0.5">
                          <span className="text-[10px] text-gray-500 font-medium uppercase tracking-wide">{a.source}</span>
                          {a.category && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-900 text-gray-500 border border-gray-800">{a.category}</span>}
                          {a.reliability > 0 && (
                            <span className="text-[10px] text-gray-600">
                              trust: {(a.reliability * 100).toFixed(0)}%
                            </span>
                          )}
                          <span className="ml-auto text-[10px] text-gray-600 flex items-center gap-0.5">
                            <Clock className="w-2.5 h-2.5" />
                            {a.published_at ? timeAgo(a.published_at) : timeAgo(a.fetched_at)}
                          </span>
                        </div>
                        <div className="flex items-center gap-2">
                          <h4 className="text-sm text-gray-200 leading-snug">
                            {a.url ? (
                              <a href={a.url} target="_blank" rel="noopener noreferrer" className="hover:text-white hover:underline">
                                {a.title}
                              </a>
                            ) : a.title}
                          </h4>
                          {a.url && (
                            <a href={a.url} target="_blank" rel="noopener noreferrer" className="shrink-0 text-gray-600 hover:text-gray-400">
                              <ExternalLink className="w-3 h-3" />
                            </a>
                          )}
                        </div>
                        {a.summary && (
                          <p className="text-[11px] text-gray-500 mt-1 line-clamp-2">{a.summary}</p>
                        )}
                        <div className="flex items-center gap-2 mt-1.5">
                          {a.symbols && a.symbols.length > 0 && a.symbols.map(s => (
                            <span key={s} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20">
                              {s}
                            </span>
                          ))}
                          {a.sentiment_score != null && (
                            <span className={`text-[10px] font-mono ${sentimentColor(a.sentiment_score)}`}>
                              {a.sentiment_label || sentimentLabel(a.sentiment_score)} ({(a.sentiment_score * 100).toFixed(0)}%)
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ═══════════════════ AI DECISIONS TAB ═══════════════════ */}
        {tab === 'decisions' && (
          <div className="space-y-3">
            {/* Filters */}
            <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <Filter className="w-3.5 h-3.5 text-gray-500" />
                <input
                  type="text"
                  placeholder="Symbol (BTC/USDT...)"
                  value={decisionSymbol}
                  onChange={e => setDecisionSymbol(e.target.value)}
                  className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300 w-36"
                />
                <select
                  value={decisionLimit}
                  onChange={e => setDecisionLimit(Number(e.target.value))}
                  className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300"
                >
                  <option value={50}>50 latest</option>
                  <option value={100}>100 latest</option>
                  <option value={200}>200 latest</option>
                  <option value={500}>500 latest</option>
                </select>
                <button
                  onClick={fetchDecisions}
                  className="px-3 py-1.5 bg-purple-600/20 border border-purple-500/30 text-purple-300 rounded text-xs hover:bg-purple-600/30 transition"
                >
                  Search
                </button>
                <span className="ml-auto text-[10px] text-gray-500">{decisions.length} decisions</span>
              </div>
            </div>

            {loading ? (
              <div className="text-center py-8 text-gray-500 text-sm">Loading decisions...</div>
            ) : decisions.length === 0 ? (
              <div className="text-center py-8 text-gray-500 text-sm">No AI decisions found</div>
            ) : (
              <div className="space-y-1">
                {decisions.map(d => {
                  const isExpanded = expandedDecision === d.id
                  return (
                    <div key={d.id} className="bg-gray-800/30 border border-gray-800 rounded-lg overflow-hidden">
                      <div
                        className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-gray-800/50 transition"
                        onClick={() => setExpandedDecision(isExpanded ? null : d.id)}
                      >
                        <span className="text-[10px] text-gray-600 w-14 shrink-0">{timeAgo(d.created_at)}</span>
                        <span className={`text-[11px] font-medium w-28 truncate ${roleColor(d.agent_role)}`}>
                          {d.agent_name}
                        </span>
                        <span className="text-xs font-mono font-medium text-white w-20">{d.symbol}</span>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${actionColor(d.action)}`}>
                          {d.action.toUpperCase()}
                        </span>
                        <span className="text-[10px] text-gray-500 font-mono w-10">
                          {(d.confidence * 100).toFixed(0)}%
                        </span>
                        {d.outcome && (
                          <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold ${
                            d.outcome === 'win' ? 'bg-green-500/10 text-green-400' :
                            d.outcome === 'loss' ? 'bg-red-500/10 text-red-400' :
                            'bg-gray-500/10 text-gray-400'
                          }`}>
                            {d.outcome.toUpperCase()}
                            {d.outcome_pnl != null && ` ${d.outcome_pnl >= 0 ? '+' : ''}$${d.outcome_pnl.toFixed(2)}`}
                          </span>
                        )}
                        <span className="text-gray-600 text-[11px] truncate flex-1">{d.reasoning?.slice(0, 60)}</span>
                        <div className="flex items-center gap-1 shrink-0">
                          {d.ai_called ? (
                            <span className="text-[9px] text-purple-400 bg-purple-500/10 px-1 rounded">AI</span>
                          ) : (
                            <span className="text-[9px] text-yellow-500 bg-yellow-500/10 px-1 rounded">MEM</span>
                          )}
                          {isExpanded ? <ChevronUp className="w-3 h-3 text-gray-500" /> : <ChevronDown className="w-3 h-3 text-gray-500" />}
                        </div>
                      </div>
                      {isExpanded && (
                        <div className="px-3 pb-3 pt-1 border-t border-gray-800/50">
                          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-2">
                            <div className="text-[10px]">
                              <span className="text-gray-600">Session:</span>{' '}
                              <span className="text-gray-400 font-mono">{d.session_id || '—'}</span>
                            </div>
                            <div className="text-[10px]">
                              <span className="text-gray-600">Confidence:</span>{' '}
                              <span className="text-white font-mono">{(d.confidence * 100).toFixed(1)}%</span>
                            </div>
                            <div className="text-[10px]">
                              <span className="text-gray-600">Method:</span>{' '}
                              <span className={d.ai_called ? 'text-purple-400' : 'text-yellow-400'}>
                                {d.ai_called ? 'OpenAI API' : 'Local Memory'}
                              </span>
                            </div>
                            <div className="text-[10px]">
                              <span className="text-gray-600">Memory Used:</span>{' '}
                              <span className="text-gray-400">{d.memory_context_used} past decisions</span>
                            </div>
                          </div>
                          <div className="bg-gray-900/50 rounded p-2.5">
                            <div className="text-[10px] text-gray-600 mb-1">Full Reasoning</div>
                            <p className="text-[11px] text-gray-300 leading-relaxed whitespace-pre-wrap">{d.reasoning}</p>
                          </div>
                          {d.outcome && (
                            <div className="mt-2 flex items-center gap-2 text-[11px]">
                              <span className="text-gray-600">Outcome:</span>
                              <span className={d.outcome === 'win' ? 'text-green-400 font-bold' : d.outcome === 'loss' ? 'text-red-400 font-bold' : 'text-gray-400'}>
                                {d.outcome.toUpperCase()}
                              </span>
                              {d.outcome_pnl != null && (
                                <>
                                  <span className={`font-mono ${d.outcome_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                                    {d.outcome_pnl >= 0 ? '+' : ''}${d.outcome_pnl.toFixed(4)}
                                  </span>
                                  {toZar(d.outcome_pnl) && (
                                    <span className="text-gray-500 text-[10px]">({toZar(d.outcome_pnl)})</span>
                                  )}
                                </>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {/* ═══════════════════ SENTIMENT TAB ═══════════════════ */}
        {tab === 'sentiment' && (
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <button
                onClick={async () => {
                  setLoading(true)
                  try {
                    await apiClient.updateSentiments()
                    await fetchSentiment()
                  } catch {} finally { setLoading(false) }
                }}
                className="px-3 py-1.5 bg-green-600/20 border border-green-500/30 text-green-300 text-xs rounded-lg hover:bg-green-600/30 transition"
                disabled={loading}
              >
                {loading ? 'Updating...' : 'Refresh Sentiment Data'}
              </button>
              <span className="text-[10px] text-gray-500">{sentiments.length} basic + {enhancedSentiments.length} enhanced scores</span>
            </div>

            {/* Sentiment Cards */}
            {enhancedSentiments.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {enhancedSentiments.map((s: any) => {
                  const score = s.score ?? s.sentiment_score ?? 0
                  const mag = s.magnitude ?? 0
                  return (
                    <div key={s.symbol} className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                      <div className="flex items-center justify-between mb-2">
                        <span className="font-mono font-bold text-lg text-white">{s.symbol}</span>
                        <span className={`text-sm font-semibold ${sentimentColor(score)}`}>
                          {sentimentLabel(score)}
                        </span>
                      </div>
                      {/* Score bar */}
                      <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-2">
                        <div
                          className={`h-full transition-all duration-300 ${score > 0 ? 'bg-green-500' : score < 0 ? 'bg-red-500' : 'bg-gray-500'}`}
                          style={{ width: `${Math.min(Math.abs(score) * 100, 100)}%` }}
                        />
                      </div>
                      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
                        <div className="flex justify-between">
                          <span className="text-gray-500">Score</span>
                          <span className={`font-mono ${sentimentColor(score)}`}>
                            {score > 0 ? '+' : ''}{(score * 100).toFixed(1)}%
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">Magnitude</span>
                          <span className="text-gray-300 font-mono">{(mag * 100).toFixed(1)}%</span>
                        </div>
                        {s.news_score != null && (
                          <div className="flex justify-between">
                            <span className="text-gray-500">News</span>
                            <span className={`font-mono ${sentimentColor(s.news_score)}`}>
                              {(s.news_score * 100).toFixed(1)}%
                            </span>
                          </div>
                        )}
                        {s.social_score != null && (
                          <div className="flex justify-between">
                            <span className="text-gray-500">Social</span>
                            <span className={`font-mono ${sentimentColor(s.social_score)}`}>
                              {(s.social_score * 100).toFixed(1)}%
                            </span>
                          </div>
                        )}
                        {s.technical_score != null && (
                          <div className="flex justify-between">
                            <span className="text-gray-500">Technical</span>
                            <span className={`font-mono ${sentimentColor(s.technical_score)}`}>
                              {(s.technical_score * 100).toFixed(1)}%
                            </span>
                          </div>
                        )}
                        <div className="flex justify-between">
                          <span className="text-gray-500">Sources</span>
                          <span className="text-gray-300 font-mono">{s.sources_count ?? 0}</span>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : sentiments.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {sentiments.map(s => (
                  <div key={s.symbol} className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                    <div className="flex items-center justify-between mb-2">
                      <span className="font-mono font-bold text-lg text-white">{s.symbol}</span>
                      <span className={`text-sm font-semibold ${sentimentColor(s.score)}`}>
                        {sentimentLabel(s.score)}
                      </span>
                    </div>
                    <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden mb-2">
                      <div
                        className={`h-full ${s.score > 0 ? 'bg-green-500' : s.score < 0 ? 'bg-red-500' : 'bg-gray-500'}`}
                        style={{ width: `${Math.min(Math.abs(s.score) * 100, 100)}%` }}
                      />
                    </div>
                    <div className="flex justify-between text-[11px]">
                      <span className="text-gray-500">
                        Score: {s.score > 0 ? '+' : ''}{(s.score * 100).toFixed(1)}%
                      </span>
                      <span className="text-gray-600">{s.sources_count} sources</span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-8 text-gray-500 text-sm">No sentiment data available</div>
            )}
          </div>
        )}

        {/* ═══════════════════ LEARNING TAB ═══════════════════ */}
        {tab === 'learning' && (
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <select
                value={decisionRole}
                onChange={e => setDecisionRole(e.target.value)}
                className="bg-gray-900 border border-gray-700 rounded px-2.5 py-1.5 text-xs text-gray-300"
              >
                <option value="">All Agents</option>
                <option value="market_analyst">Market Analyst</option>
                <option value="signal_generator">Signal Generator</option>
                <option value="risk_manager">Risk Manager</option>
                <option value="trade_executor">Trade Executor</option>
                <option value="sentiment_analyst">Sentiment Analyst</option>
                <option value="position_reviewer">Position Reviewer</option>
              </select>
              <button
                onClick={fetchLearning}
                className="px-3 py-1.5 bg-yellow-600/20 border border-yellow-500/30 text-yellow-300 text-xs rounded hover:bg-yellow-600/30 transition"
              >
                Load Stats
              </button>
            </div>

            {loading ? (
              <div className="text-center py-8 text-gray-500 text-sm">Loading learning data...</div>
            ) : !learningStats ? (
              <div className="text-center py-8 text-gray-500 text-sm">No learning data available yet</div>
            ) : (
              <div className="space-y-4">
                {/* Summary Cards */}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  {learningStats.total_decisions != null && (
                    <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                      <div className="text-xs text-gray-500 mb-1">Total Decisions</div>
                      <div className="text-2xl font-bold text-white">{learningStats.total_decisions}</div>
                    </div>
                  )}
                  {learningStats.ai_calls != null && (
                    <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                      <div className="text-xs text-gray-500 mb-1">AI API Calls</div>
                      <div className="text-2xl font-bold text-purple-400">{learningStats.ai_calls}</div>
                      {learningStats.total_decisions > 0 && (
                        <div className="text-[10px] text-gray-600 mt-1">
                          {((learningStats.ai_calls / learningStats.total_decisions) * 100).toFixed(1)}% of total
                        </div>
                      )}
                    </div>
                  )}
                  {learningStats.memory_decisions != null && (
                    <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                      <div className="text-xs text-gray-500 mb-1">Memory Decisions</div>
                      <div className="text-2xl font-bold text-yellow-400">{learningStats.memory_decisions}</div>
                      <div className="text-[10px] text-gray-600 mt-1">
                        Saved {learningStats.memory_decisions} API calls
                      </div>
                    </div>
                  )}
                  {learningStats.win_rate != null && (
                    <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                      <div className="text-xs text-gray-500 mb-1">Win Rate</div>
                      <div className={`text-2xl font-bold ${learningStats.win_rate >= 50 ? 'text-green-400' : 'text-red-400'}`}>
                        {learningStats.win_rate.toFixed(1)}%
                      </div>
                      {learningStats.outcomes_recorded != null && (
                        <div className="text-[10px] text-gray-600 mt-1">
                          from {learningStats.outcomes_recorded} recorded outcomes
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* Per-Agent Breakdown */}
                {learningStats.per_agent && Object.keys(learningStats.per_agent).length > 0 && (
                  <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                    <h3 className="text-sm font-semibold text-white mb-3">Per-Agent Performance</h3>
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-gray-500 border-b border-gray-700">
                            <th className="text-left py-2 px-2">Agent</th>
                            <th className="text-right py-2 px-2">Decisions</th>
                            <th className="text-right py-2 px-2">AI Calls</th>
                            <th className="text-right py-2 px-2">Memory</th>
                            <th className="text-right py-2 px-2">Win Rate</th>
                            <th className="text-right py-2 px-2">Avg Confidence</th>
                          </tr>
                        </thead>
                        <tbody>
                          {Object.entries(learningStats.per_agent as Record<string, any>).map(([name, stats]) => (
                            <tr key={name} className="border-b border-gray-800">
                              <td className={`py-2 px-2 font-medium ${roleColor(name)}`}>{name}</td>
                              <td className="py-2 px-2 text-right font-mono text-gray-300">{(stats as any).total}</td>
                              <td className="py-2 px-2 text-right font-mono text-purple-400">{(stats as any).ai_calls}</td>
                              <td className="py-2 px-2 text-right font-mono text-yellow-400">{(stats as any).memory}</td>
                              <td className={`py-2 px-2 text-right font-mono font-semibold ${
                                ((stats as any).win_rate ?? 0) >= 50 ? 'text-green-400' : 'text-red-400'
                              }`}>
                                {(stats as any).win_rate != null ? `${(stats as any).win_rate.toFixed(1)}%` : '—'}
                              </td>
                              <td className="py-2 px-2 text-right font-mono text-gray-400">
                                {(stats as any).avg_confidence != null ? `${((stats as any).avg_confidence * 100).toFixed(0)}%` : '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* Per-Symbol Breakdown */}
                {learningStats.per_symbol && Object.keys(learningStats.per_symbol).length > 0 && (
                  <div className="bg-gray-800/40 border border-gray-700 rounded-lg p-4">
                    <h3 className="text-sm font-semibold text-white mb-3">Per-Symbol Performance</h3>
                    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2">
                      {Object.entries(learningStats.per_symbol as Record<string, any>)
                        .sort(([, a], [, b]) => ((b as any).total ?? 0) - ((a as any).total ?? 0))
                        .map(([sym, stats]) => (
                          <div key={sym} className="bg-gray-900/50 rounded-lg p-2.5 border border-gray-800">
                            <div className="font-mono font-bold text-sm text-white mb-0.5">{sym}</div>
                            <div className="text-[10px] text-gray-500">
                              {(stats as any).total} decisions
                            </div>
                            {(stats as any).win_rate != null && (
                              <div className={`text-[11px] font-mono font-semibold ${
                                (stats as any).win_rate >= 50 ? 'text-green-400' : 'text-red-400'
                              }`}>
                                {(stats as any).win_rate.toFixed(1)}% win
                              </div>
                            )}
                          </div>
                        ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  )
}
