import Head from 'next/head'
import { useCallback, useEffect, useRef, useState } from 'react'
import { apiClient } from '@/services/api'
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  ExternalLink,
  Link,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Settings,
  Trash2,
  Twitter,
  X,
  Zap,
} from 'lucide-react'

// ── types ──────────────────────────────────────────────────────────────────

interface XAccount {
  username: string
  display_name: string
  symbols: string[]
  tier: 1 | 2 | 3
  reliability: number
  builtin: boolean
  active: boolean
}

interface XTweet {
  username: string
  display: string
  symbols: string[]
  tier: number
  reliability: number
  tweet_id: string
  text: string
  created_at: string
  fetched_at: string
}

interface XConfig {
  bearer_token_set: boolean
  oauth1_set: boolean
  auth_mode: 'oauth1' | 'bearer' | 'none'
  interval_seconds: number
  enabled: boolean
  active_account_count: number
  total_account_count: number
}

interface XStatus {
  running: boolean
  bearer_token_set: boolean
  oauth1_set: boolean
  auth_mode: 'oauth1' | 'bearer' | 'none'
  last_run: string | null
  total_tweets_seen: number
  active_accounts: number
  cached_tweets: number
}

const TIER_LABELS: Record<number, string> = { 1: 'Critical', 2: 'Important', 3: 'Watch' }
const TIER_COLORS: Record<number, string> = {
  1: 'text-red-400 bg-red-500/10 border-red-500/30',
  2: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/30',
  3: 'text-blue-400 bg-blue-500/10 border-blue-500/30',
}

const INTERVAL_OPTIONS = [
  { label: '1 min',  value: 60  },
  { label: '2 min',  value: 120 },
  { label: '5 min',  value: 300 },
  { label: '10 min', value: 600 },
]

// ── component ──────────────────────────────────────────────────────────────

export default function XMonitorPage() {
  const [config, setConfig]         = useState<XConfig | null>(null)
  const [status, setStatus]         = useState<XStatus | null>(null)
  const [accounts, setAccounts]     = useState<XAccount[]>([])
  const [feed, setFeed]             = useState<XTweet[]>([])
  const [loading, setLoading]       = useState(true)
  const [saving, setSaving]         = useState(false)
  const [polling, setPolling]       = useState(false)
  const [testing, setTesting]       = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; mode?: string; username?: string; name?: string; note?: string; error?: string } | null>(null)
  const [togglingLoop, setTogglingLoop] = useState(false)
  const [error, setError]           = useState<string | null>(null)
  const [success, setSuccess]       = useState<string | null>(null)

  // add-account form
  const [showAddForm, setShowAddForm] = useState(false)
  const [addUsername, setAddUsername] = useState('')
  const [addDisplay, setAddDisplay]   = useState('')
  const [addSymbols, setAddSymbols]   = useState('')
  const [addTier, setAddTier]         = useState(2)
  const [addReliability, setAddReliability] = useState(0.80)
  const [addError, setAddError]       = useState<string | null>(null)
  const [addSaving, setAddSaving]     = useState(false)

  // interval editor
  const [editInterval, setEditInterval] = useState<number | null>(null)
  const [showSetup, setShowSetup]       = useState(false)
  const [copied, setCopied]             = useState<string | null>(null)

  // tab navigation
  const [activeTab, setActiveTab] = useState<'monitor' | 'scraper'>('monitor')

  const feedRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // ── loaders ──────────────────────────────────────────────

  const loadAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [cfgRes, statusRes, accRes, feedRes] = await Promise.all([
        apiClient.getXMonitorConfig(),
        apiClient.getXMonitorStatus(),
        apiClient.getXMonitorAccounts(),
        apiClient.getXMonitorFeed(20),
      ])
      setConfig(cfgRes.data)
      setStatus(statusRes.data)
      setAccounts(accRes.data.accounts ?? [])
      setFeed(feedRes.data.tweets ?? [])
      setEditInterval(cfgRes.data.interval_seconds)
    } catch {
      setError('Failed to load X Monitor data — is the backend running?')
    } finally {
      setLoading(false)
    }
  }, [])

  const refreshStatus = useCallback(async () => {
    try {
      const [statusRes, feedRes] = await Promise.all([
        apiClient.getXMonitorStatus(),
        apiClient.getXMonitorFeed(20),
      ])
      setStatus(statusRes.data)
      setFeed(feedRes.data.tweets ?? [])
    } catch {}
  }, [])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  // auto-refresh feed every 30 s
  useEffect(() => {
    feedRef.current = setInterval(refreshStatus, 30_000)
    return () => { if (feedRef.current) clearInterval(feedRef.current) }
  }, [refreshStatus])

  // ── flash helpers ─────────────────────────────────────────

  const flash = (msg: string) => {
    setSuccess(msg)
    setTimeout(() => setSuccess(null), 3000)
  }

  // ── actions ───────────────────────────────────────────────

  const handleToggleLoop = async () => {
    if (!status) return
    setTogglingLoop(true)
    try {
      if (status.running) {
        await apiClient.stopXMonitor()
        flash('Monitor stopped')
      } else {
        await apiClient.startXMonitor()
        flash('Monitor started')
      }
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? 'Failed to toggle loop')
    } finally {
      setTogglingLoop(false)
    }
  }

  const handleSaveInterval = async () => {
    if (editInterval === null) return
    setSaving(true)
    try {
      await apiClient.updateXMonitorConfig({ interval_seconds: editInterval })
      setConfig(prev => prev ? { ...prev, interval_seconds: editInterval } : prev)
      flash('Interval saved')
    } catch {
      setError('Failed to save interval')
    } finally {
      setSaving(false)
    }
  }

  const handleToggleAccount = async (username: string, currentlyActive: boolean) => {
    try {
      await apiClient.updateXMonitorAccount(username, { active: !currentlyActive })
      setAccounts(prev =>
        prev.map(a => a.username === username ? { ...a, active: !currentlyActive } : a)
      )
    } catch {
      setError(`Failed to update @${username}`)
    }
  }

  const handleDeleteAccount = async (username: string) => {
    if (!confirm(`Remove @${username} from the watch list?`)) return
    try {
      await apiClient.removeXMonitorAccount(username)
      setAccounts(prev => prev.filter(a => a.username !== username))
      flash(`@${username} removed`)
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? `Failed to remove @${username}`)
    }
  }

  const handleTestConnection = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const res = await apiClient.testXMonitorConnection()
      setTestResult(res.data)
    } catch {
      setTestResult({ ok: false, error: 'Request failed — is the backend running?' })
    } finally {
      setTesting(false)
    }
  }

  const handlePollNow = async () => {    setPolling(true)
    try {
      const res = await apiClient.pollXMonitorNow()
      flash(`Polled — ${res.data.new_tweets} new tweet(s)`)
      await refreshStatus()
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? 'Poll failed')
    } finally {
      setPolling(false)
    }
  }

  const handleAddAccount = async () => {
    const username = addUsername.replace(/^@/, '').trim()
    if (!username) { setAddError('Username is required'); return }
    setAddSaving(true)
    setAddError(null)
    try {
      await apiClient.addXMonitorAccount({
        username,
        display_name: addDisplay || username,
        symbols: addSymbols.split(',').map(s => s.trim()).filter(Boolean),
        tier: addTier,
        reliability: addReliability,
      })
      flash(`@${username} added`)
      setAddUsername('')
      setAddDisplay('')
      setAddSymbols('')
      setAddTier(2)
      setAddReliability(0.80)
      setShowAddForm(false)
      const res = await apiClient.getXMonitorAccounts()
      setAccounts(res.data.accounts ?? [])
    } catch (e: any) {
      setAddError(e?.response?.data?.detail ?? 'Failed to add account')
    } finally {
      setAddSaving(false)
    }
  }

  // ── render ────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="flex items-center gap-3 text-gray-400">
          <RefreshCw className="w-5 h-5 animate-spin" />
          Loading X Monitor…
        </div>
      </div>
    )
  }

  const isRunning  = status?.running ?? false
  const authMode   = config?.auth_mode ?? 'none'
  const hasAuth    = authMode !== 'none'

  const tierOneAccounts   = accounts.filter(a => a.tier === 1)
  const tierTwoAccounts   = accounts.filter(a => a.tier === 2)
  const tierThreeAccounts = accounts.filter(a => a.tier === 3)

  return (
    <>
      <Head><title>TradeBot — X Monitor</title></Head>

      <div className="space-y-6 max-w-7xl mx-auto pb-12">

        {/* ── Header ── */}
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-3xl font-bold text-white flex items-center gap-3">
              <Twitter className="w-8 h-8 text-sky-400" />
              X (Twitter) Market Monitor
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Polls market-moving accounts every {config?.interval_seconds ?? 120}s and feeds breaking tweets
              directly into the sentiment pipeline and trading agents.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={handleTestConnection}
              disabled={testing}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/30 text-sky-400 text-sm disabled:opacity-40 transition-colors"
            >
              <Activity className={`w-4 h-4 ${testing ? 'animate-pulse' : ''}`} />
              {testing ? 'Testing…' : 'Test connection'}
            </button>

            <button
              onClick={handlePollNow}
              disabled={polling || !hasAuth}
              className="flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-white text-sm disabled:opacity-40 transition-colors"
            >
              <RefreshCw className={`w-4 h-4 ${polling ? 'animate-spin' : ''}`} />
              Poll now
            </button>

            <button
              onClick={handleToggleLoop}
              disabled={togglingLoop || !hasAuth}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-40 ${
                isRunning
                  ? 'bg-red-600/20 hover:bg-red-600/30 text-red-400 border border-red-500/30'
                  : 'bg-green-600/20 hover:bg-green-600/30 text-green-400 border border-green-500/30'
              }`}
            >
              {isRunning ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
              {isRunning ? 'Stop monitor' : 'Start monitor'}
            </button>
          </div>
        </div>

        {/* ── Tab bar ── */}
        <div className="flex border-b border-gray-700 -mb-2">
          {(['monitor', 'scraper'] as const).map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`flex items-center gap-1.5 px-5 py-3 text-sm font-medium transition-colors border-b-2 -mb-px ${
                activeTab === tab
                  ? 'border-sky-400 text-sky-400'
                  : 'border-transparent text-gray-400 hover:text-gray-200'
              }`}
            >
              {tab === 'monitor' ? <Twitter className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
              {tab === 'monitor' ? 'Monitor' : 'AI Scraper'}
            </button>
          ))}
        </div>

        {/* ── SCRAPER TAB ── */}
        {activeTab === 'scraper' && (
          <ScraperTab accounts={accounts} />
        )}

        {/* ── MONITOR TAB ── */}
        {activeTab === 'monitor' && (<>

        {/* ── Flash messages ── */}
        {error && (
          <div className="flex items-center gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            {error}
            <button onClick={() => setError(null)} className="ml-auto"><X className="w-4 h-4" /></button>
          </div>
        )}
        {success && (
          <div className="flex items-center gap-2 p-3 rounded-lg bg-green-500/10 border border-green-500/30 text-green-400 text-sm">
            <CheckCircle2 className="w-4 h-4 shrink-0" />
            {success}
          </div>
        )}

        {/* ── Status cards ── */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <StatusCard
            label="Auth Mode"
            value={
              authMode === 'oauth1' ? 'OAuth 1.0a ✓' :
              authMode === 'bearer' ? 'Bearer Token ✓' :
              'Not configured ✗'
            }
            color={
              authMode !== 'none' ? 'text-green-400' : 'text-red-400'
            }
          />
          <StatusCard
            label="Loop Status"
            value={isRunning ? 'Running' : 'Stopped'}
            color={isRunning ? 'text-green-400' : 'text-gray-400'}
          />
          <StatusCard
            label="Active Accounts"
            value={`${status?.active_accounts ?? 0}`}
          />
          <StatusCard
            label="Tweets Seen"
            value={`${status?.total_tweets_seen ?? 0}`}
          />
        </div>

        {/* ── Test connection result ── */}
        {testResult && (
          <div className={`flex items-start gap-3 p-4 rounded-lg text-sm border ${
            testResult.ok
              ? 'bg-green-500/10 border-green-500/30 text-green-400'
              : 'bg-red-500/10 border-red-500/30 text-red-400'
          }`}>
            {testResult.ok
              ? <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
              : <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />}
            <div className="flex-1">
              {testResult.ok ? (
                <span>
                  {testResult.mode === 'oauth1'
                    ? <><strong className="text-white">OAuth 1.0a verified</strong> — authenticated as{' '}<strong className="text-white">@{testResult.username}</strong> · home-timeline monitoring ready (free tier)</>  
                    : <><strong className="text-white">Bearer token verified</strong> — app-only auth active</>}
                </span>
              ) : (
                <div className="space-y-1">
                  <span>{testResult.error}</span>
                  {testResult.error?.includes('403') && testResult.mode === 'oauth1' && (
                    <p className="text-xs text-amber-400 mt-1">
                      → Your app needs to be enrolled in a Twitter Developer Project — see the setup panel below.
                    </p>
                  )}
                </div>
              )}
            </div>
            <button onClick={() => setTestResult(null)} className="ml-auto shrink-0">
              <X className="w-4 h-4" />
            </button>
          </div>
        )}

        {/* ── Auth setup panel ── */}
        <AuthSetupPanel
          authMode={authMode}
          open={showSetup || authMode === 'none'}
          onToggle={() => setShowSetup(v => !v)}
          copied={copied}
          onCopy={(text, key) => {
            navigator.clipboard.writeText(text).catch(() => {})
            setCopied(key)
            setTimeout(() => setCopied(null), 2000)
          }}
          onTestConnection={handleTestConnection}
          testing={testing}
        />

        {/* ── Poll interval ── */}
        <section className="bg-gray-800/40 border border-gray-700 rounded-xl p-5 space-y-3">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Zap className="w-4 h-4 text-yellow-400" />
            Poll Interval
          </h2>
          <div className="flex items-center gap-3 flex-wrap">
            {INTERVAL_OPTIONS.map(opt => (
              <button
                key={opt.value}
                onClick={() => setEditInterval(opt.value)}
                className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${
                  editInterval === opt.value
                    ? 'bg-tradebot-accent/20 border-tradebot-accent text-tradebot-accent'
                    : 'bg-gray-700 border-gray-600 text-gray-300 hover:border-gray-500'
                }`}
              >
                {opt.label}
              </button>
            ))}
            <button
              onClick={handleSaveInterval}
              disabled={saving || editInterval === config?.interval_seconds}
              className="ml-auto px-4 py-1.5 rounded-lg bg-tradebot-accent text-white text-sm font-medium disabled:opacity-40 hover:opacity-90 transition-opacity"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
          <p className="text-xs text-gray-500">
            The monitor polls all active accounts and injects new tweets into the sentiment agent pipeline.
            2 min is the recommended default to stay well within Twitter's free-tier rate limits.
          </p>
        </section>

        {/* ── Accounts table ── */}
        <section className="bg-gray-800/40 border border-gray-700 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Activity className="w-4 h-4 text-sky-400" />
              Watched Accounts ({accounts.length})
            </h2>
            <button
              onClick={() => { setShowAddForm(v => !v); setAddError(null) }}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-sky-500/15 border border-sky-500/30 text-sky-400 hover:bg-sky-500/25 text-sm transition-colors"
            >
              {showAddForm ? <X className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}
              {showAddForm ? 'Cancel' : 'Add Account'}
            </button>
          </div>

          {/* Add form */}
          {showAddForm && (
            <div className="px-5 py-4 bg-gray-800/60 border-b border-gray-700 space-y-3">
              {addError && (
                <p className="text-sm text-red-400">{addError}</p>
              )}
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                <div>
                  <label className="block text-xs text-gray-400 mb-1">@Username *</label>
                  <input
                    className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                    placeholder="e.g. coinbase"
                    value={addUsername}
                    onChange={e => setAddUsername(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Display Name</label>
                  <input
                    className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                    placeholder="e.g. Coinbase"
                    value={addDisplay}
                    onChange={e => setAddDisplay(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Symbols (comma-sep)</label>
                  <input
                    className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                    placeholder="BTC, ETH, _MARKET_"
                    value={addSymbols}
                    onChange={e => setAddSymbols(e.target.value)}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Tier</label>
                  <select
                    className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                    value={addTier}
                    onChange={e => setAddTier(Number(e.target.value))}
                  >
                    <option value={1}>1 — Critical (Trump, Musk, Fed)</option>
                    <option value={2}>2 — Important (CZ, Vitalik, GS)</option>
                    <option value={3}>3 — Watch (general commentary)</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1">Reliability: {(addReliability * 100).toFixed(0)}%</label>
                  <input
                    type="range"
                    min={0.5} max={1.0} step={0.05}
                    value={addReliability}
                    onChange={e => setAddReliability(Number(e.target.value))}
                    className="w-full accent-sky-500"
                  />
                </div>
                <div className="flex items-end">
                  <button
                    onClick={handleAddAccount}
                    disabled={addSaving}
                    className="w-full px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-500 text-white text-sm font-medium disabled:opacity-40 transition-colors"
                  >
                    {addSaving ? 'Adding…' : 'Add Account'}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Account rows */}
          <div className="divide-y divide-gray-700/50">
            {[
              { tier: 1, list: tierOneAccounts },
              { tier: 2, list: tierTwoAccounts },
              { tier: 3, list: tierThreeAccounts },
            ].map(({ tier, list }) => list.length === 0 ? null : (
              <div key={tier}>
                <div className="px-5 py-2 bg-gray-900/30">
                  <span className={`text-xs font-semibold px-2 py-0.5 rounded border ${TIER_COLORS[tier]}`}>
                    Tier {tier} — {TIER_LABELS[tier]}
                  </span>
                </div>
                {list.map(account => (
                  <AccountRow
                    key={account.username}
                    account={account}
                    onToggle={() => handleToggleAccount(account.username, account.active)}
                    onDelete={() => handleDeleteAccount(account.username)}
                  />
                ))}
              </div>
            ))}
          </div>
        </section>

        {/* ── Tweet feed ── */}
        <section className="bg-gray-800/40 border border-gray-700 rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-gray-700">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Twitter className="w-4 h-4 text-sky-400" />
              Recent Tweets Feed
              {status?.last_run && (
                <span className="text-xs text-gray-500 font-normal">
                  Last poll: {new Date(status.last_run).toLocaleTimeString()}
                </span>
              )}
            </h2>
            <button
              onClick={refreshStatus}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs transition-colors"
            >
              <RefreshCw className="w-3.5 h-3.5" /> Refresh
            </button>
          </div>

          {feed.length === 0 ? (
            <div className="px-5 py-10 text-center text-gray-500 text-sm">
              No tweets captured yet.{' '}
              {authMode === 'none'
                ? 'Configure Twitter credentials and start the monitor.'
                : isRunning
                ? 'Waiting for the next poll cycle…'
                : 'Start the monitor to begin capturing tweets.'}
            </div>
          ) : (
            <div className="divide-y divide-gray-700/30 max-h-[600px] overflow-y-auto">
              {feed.map((tweet, i) => (
                <TweetCard key={`${tweet.tweet_id}-${i}`} tweet={tweet} />
              ))}
            </div>
          )}
        </section>

        </>)}  {/* end monitor tab */}

      </div>
    </>
  )
}

// ── ScraperTab ─────────────────────────────────────────────────────────────

interface ScraperStatus {
  installed: boolean    // always true — basic mode works without any install
  ai_mode: boolean      // true when scrapegraphai is installed
  installed_version: string | null
  latest_version: string | null
  update_available: boolean
  last_version_check: string | null
  provider: string
  model: string
  dedicated_key_set: boolean
  results_count: number
  last_install_error: string | null
  python_executable: string | null
  scrape_mode: 'ai' | 'basic'
}

interface ScrapeResult {
  username: string
  links: string[]
  website: string | null
  bio: string | null
  error?: string
  scraped_at: string
}

function ScraperTab({ accounts }: { accounts: Array<{ username: string; display_name: string }> }) {
  const [status, setStatus]           = useState<ScraperStatus | null>(null)
  const [results, setResults]         = useState<ScrapeResult[]>([])
  const [loading, setLoading]         = useState(true)
  const [installing, setInstalling]   = useState(false)
  const [installError, setInstallError] = useState<string | null>(null)
  const [scraping, setScraping]       = useState(false)
  const [scrapingUser, setScrapingUser] = useState<string | null>(null)
  const [showConfig, setShowConfig]   = useState(false)
  const [cfgKey, setCfgKey]           = useState('')
  const [cfgUrl, setCfgUrl]           = useState('')
  const [cfgModel, setCfgModel]       = useState('')
  const [savingCfg, setSavingCfg]     = useState(false)
  const [expanded, setExpanded]       = useState<string | null>(null)
  const [msg, setMsg]                 = useState<{ ok: boolean; text: string } | null>(null)
  const flashRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const flash = (ok: boolean, text: string) => {
    setMsg({ ok, text })
    if (flashRef.current) clearTimeout(flashRef.current)
    flashRef.current = setTimeout(() => setMsg(null), 4000)
  }

  const loadAll = useCallback(async () => {
    setLoading(true)
    try {
      const [sRes, rRes] = await Promise.all([
        apiClient.getXScraperStatus(),
        apiClient.getXScraperResults(),
      ])
      setStatus(sRes.data)
      setResults(rRes.data.results ?? [])
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  const handleInstall = async () => {
    setInstalling(true)
    setInstallError(null)
    try {
      const res = await apiClient.installXScraper()
      if (res.data.ok) {
        setInstallError(null)
        flash(true, `ScrapeGraphAI v${res.data.installed_version} installed ✓`)
        await loadAll()
      } else {
        setInstallError(res.data.error ?? 'Install failed — check error below')
      }
    } catch (e: any) {
      setInstallError(e?.response?.data?.detail ?? String(e))
    }
    setInstalling(false)
  }

  const handleScrapeAll = async () => {
    setScraping(true)
    const res = await apiClient.scrapeAllXAccounts()
    flash(true, `Scraping ${res.data.queued} accounts in background — refresh in ~30 s`)
    setTimeout(async () => { await loadAll(); setScraping(false) }, 35_000)
  }

  const handleScrapeOne = async (username: string) => {
    setScrapingUser(username)
    try {
      await apiClient.scrapeXAccount(username)
      flash(true, `@${username} scraped`)
      const rRes = await apiClient.getXScraperResults()
      setResults(rRes.data.results ?? [])
    } catch (e: any) {
      flash(false, e?.response?.data?.detail ?? 'Scrape failed')
    }
    setScrapingUser(null)
  }

  const handleSaveCfg = async () => {
    setSavingCfg(true)
    const res = await apiClient.updateXScraperConfig({
      api_key:  cfgKey  || undefined,
      base_url: cfgUrl  || undefined,
      model:    cfgModel || undefined,
    })
    setSavingCfg(false)
    flash(true, `Provider updated: ${res.data.provider}`)
    await loadAll()
  }

  const resultMap = Object.fromEntries(results.map(r => [r.username, r]))

  if (loading) {
    return (
      <div className="flex items-center justify-center h-40 text-gray-400 gap-2">
        <RefreshCw className="w-4 h-4 animate-spin" /> Loading scraper status…
      </div>
    )
  }

  return (
    <div className="space-y-5">

      {/* flash */}
      {msg && (
        <div className={`flex items-center gap-2 p-3 rounded-lg text-sm border ${
          msg.ok
            ? 'bg-green-500/10 border-green-500/30 text-green-400'
            : 'bg-red-500/10 border-red-500/30 text-red-400'
        }`}>
          {msg.ok ? <CheckCircle2 className="w-4 h-4 shrink-0" /> : <AlertTriangle className="w-4 h-4 shrink-0" />}
          {msg.text}
        </div>
      )}

      {/* persistent install error */}
      {installError && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/5 p-4 space-y-3">
          <p className="text-sm font-semibold text-red-300 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0" />
            Install failed — full error:
          </p>
          <pre className="text-xs text-red-300/80 bg-black/30 rounded-lg p-3 overflow-x-auto max-h-48 whitespace-pre-wrap">{installError}</pre>
          <div className="space-y-1">
            <p className="text-xs text-gray-400 font-semibold">Run this manually in your terminal to fix it:</p>
            {status?.python_executable && (
              <div className="flex items-center gap-2 p-2.5 rounded-lg bg-gray-900 border border-gray-700">
                <code className="text-xs font-mono text-green-400 flex-1 break-all">
                  {status.python_executable} -m pip install --upgrade scrapegraphai
                </code>
                <button
                  onClick={() => navigator.clipboard.writeText(`${status.python_executable} -m pip install --upgrade scrapegraphai`).catch(() => {})}
                  className="shrink-0 p-1 rounded text-gray-400 hover:text-white transition-colors"
                  title="Copy"
                >
                  <Copy className="w-3.5 h-3.5" />
                </button>
              </div>
            )}
          </div>
          <button onClick={() => setInstallError(null)} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">Dismiss</button>
        </div>
      )}

      {/* ── status row ── */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {/* connection card */}
        <div className={`p-4 rounded-xl border flex items-start gap-3 ${
          status?.ai_mode
            ? 'bg-green-500/10 border-green-500/30'
            : 'bg-sky-500/10 border-sky-500/30'
        }`}>
          <div className="mt-0.5">
            {status?.ai_mode
              ? <CheckCircle2 className="w-5 h-5 text-green-400" />
              : <Bot className="w-5 h-5 text-sky-400" />}
          </div>
          <div className="flex-1 min-w-0">
            <p className={`text-sm font-semibold ${status?.ai_mode ? 'text-green-300' : 'text-sky-300'}`}>
              {status?.ai_mode
                ? `AI Mode — ScrapeGraphAI v${status.installed_version}`
                : 'Basic Mode — ready to scrape (stdlib only)'}
            </p>
            <p className="text-xs text-gray-400 mt-0.5">
              {status?.ai_mode
                ? `LLM-powered extraction  ·  ${status.results_count} accounts scraped`
                : 'Uses Twitter syndication API — upgrade to AI mode for deeper extraction'}
            </p>
            {status?.update_available && (
              <p className="text-xs text-sky-400 mt-1">Update available: v{status.latest_version}</p>
            )}
            {!status?.ai_mode && status?.last_install_error && !installError && (
              <p className="text-xs text-red-400 mt-1 line-clamp-2">{status.last_install_error.slice(0, 120)}…</p>
            )}
          </div>
          {!status?.ai_mode ? (
            <button
              onClick={handleInstall}
              disabled={installing}
              className="shrink-0 px-3 py-1.5 rounded-lg bg-sky-500/20 border border-sky-500/40 text-sky-300 text-xs font-medium hover:bg-sky-500/30 disabled:opacity-40 transition-colors whitespace-nowrap"
            >
              {installing ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : '⬆ Upgrade to AI mode'}
            </button>
          ) : status.update_available ? (
            <button
              onClick={handleInstall}
              disabled={installing}
              className="shrink-0 px-3 py-1.5 rounded-lg bg-sky-500/20 border border-sky-500/30 text-sky-300 text-xs font-medium hover:bg-sky-500/30 disabled:opacity-40 transition-colors"
            >
              {installing ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : 'Update'}
            </button>
          ) : null}
        </div>

        {/* provider card */}
        <div className="p-4 rounded-xl border bg-gray-800/40 border-gray-700">
          <p className="text-xs text-gray-400 mb-1">AI Provider</p>
          <p className="text-sm font-semibold text-white truncate">{status?.provider ?? '—'}</p>
          <p className="text-xs text-gray-500 mt-0.5 truncate">{status?.model || 'default model'}</p>
          {status?.dedicated_key_set && (
            <span className="mt-1.5 inline-block text-xs px-1.5 py-0.5 rounded bg-sky-500/15 border border-sky-500/30 text-sky-400">
              dedicated key
            </span>
          )}
        </div>

        {/* actions card */}
        <div className="p-4 rounded-xl border bg-gray-800/40 border-gray-700 flex flex-col gap-2 justify-center">
          <button
            onClick={handleScrapeAll}
            disabled={scraping}
            className="flex items-center justify-center gap-2 px-4 py-2 rounded-lg bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/30 text-sky-400 text-sm font-medium disabled:opacity-40 transition-colors"
          >
            <Bot className={`w-4 h-4 ${scraping ? 'animate-pulse' : ''}`} />
            {scraping ? 'Scraping…' : `Scrape all ${accounts.length} accounts`}
            {status && <span className="text-xs opacity-60">({status.scrape_mode})</span>}
          </button>
          <button
            onClick={loadAll}
            className="flex items-center justify-center gap-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
          >
            <RefreshCw className="w-3 h-3" /> Refresh
          </button>
        </div>
      </div>

      {/* ── configure AI provider ── */}
      <section className="rounded-xl border border-gray-700 bg-gray-800/40 overflow-hidden">
        <button
          onClick={() => setShowConfig(v => !v)}
          className="w-full flex items-center justify-between px-5 py-4 text-left"
        >
          <span className="text-sm font-semibold text-white flex items-center gap-2">
            <Settings className="w-4 h-4 text-gray-400" />
            Configure Dedicated AI Provider
            {status?.dedicated_key_set && (
              <span className="text-xs font-normal text-sky-400">(active)</span>
            )}
          </span>
          {showConfig
            ? <ChevronUp className="w-4 h-4 text-gray-400 shrink-0" />
            : <ChevronDown className="w-4 h-4 text-gray-400 shrink-0" />}
        </button>

        {showConfig && (
          <div className="px-5 pb-5 border-t border-gray-700/50 space-y-4 pt-4">
            <p className="text-xs text-gray-400">
              Optionally configure a separate AI provider just for the scraper — keeps scraping
              costs isolated from your main trading agents. Leave blank to use the system
              <strong className="text-gray-300"> NVIDIA NIM</strong> credentials.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">API Key</label>
                <input
                  type="password"
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                  placeholder="sk-… or nvapi-…"
                  value={cfgKey}
                  onChange={e => setCfgKey(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Base URL (optional)</label>
                <input
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                  placeholder="https://api.openai.com/v1"
                  value={cfgUrl}
                  onChange={e => setCfgUrl(e.target.value)}
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Model (optional)</label>
                <input
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-sky-500"
                  placeholder="gpt-4o-mini"
                  value={cfgModel}
                  onChange={e => setCfgModel(e.target.value)}
                />
              </div>
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={handleSaveCfg}
                disabled={savingCfg}
                className="px-4 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-white text-sm font-medium disabled:opacity-40 transition-colors"
              >
                {savingCfg ? 'Saving…' : 'Save'}
              </button>
              <p className="text-xs text-gray-500">
                Changes are applied immediately to the running server. Update <code className="bg-gray-800 px-1 rounded">.env</code> for permanence.
              </p>
            </div>
          </div>
        )}
      </section>

      {/* ── results table ── */}
      <section className="rounded-xl border border-gray-700 bg-gray-800/40 overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-700 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Link className="w-4 h-4 text-sky-400" />
            Scraped Social Links ({results.length} of {accounts.length} accounts)
          </h2>
        </div>

        {accounts.length === 0 ? (
          <p className="px-5 py-8 text-center text-gray-500 text-sm">No accounts configured.</p>
        ) : (
          <div className="divide-y divide-gray-700/40">
            {accounts.map(acct => {
              const r = resultMap[acct.username]
              const isExpanded = expanded === acct.username
              const isScraping = scrapingUser === acct.username
              return (
                <div key={acct.username}>
                  <div className="flex items-center gap-3 px-5 py-3 hover:bg-gray-800/30 transition-colors">
                    <div className="w-8 h-8 rounded-full bg-sky-500/15 border border-sky-500/30 flex items-center justify-center shrink-0">
                      <Twitter className="w-3.5 h-3.5 text-sky-400" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-white">@{acct.username}</p>
                      {r && !r.error ? (
                        <p className="text-xs text-gray-400">
                          {r.website
                            ? <a href={r.website} target="_blank" rel="noopener noreferrer"
                                className="text-sky-400 hover:underline truncate max-w-xs inline-block">{r.website}</a>
                            : <span className="text-gray-500">No primary website</span>}
                          {' · '}
                          <span>{r.links?.length ?? 0} link{(r.links?.length ?? 0) !== 1 ? 's' : ''} found</span>
                        </p>
                      ) : r?.error ? (
                        <p className="text-xs text-red-400 truncate">{r.error}</p>
                      ) : (
                        <p className="text-xs text-gray-500">Not yet scraped</p>
                      )}
                    </div>
                    {r?.scraped_at && (
                      <span className="text-xs text-gray-500 shrink-0 hidden sm:block">
                        {new Date(r.scraped_at).toLocaleDateString()}
                      </span>
                    )}
                    <div className="flex items-center gap-1.5 shrink-0">
                      {r?.links?.length ? (
                        <button
                          onClick={() => setExpanded(v => v === acct.username ? null : acct.username)}
                          className="p-1.5 rounded-lg text-gray-400 hover:text-sky-400 hover:bg-sky-500/10 transition-colors"
                          title={isExpanded ? 'Collapse' : 'Show links'}
                        >
                          {isExpanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                        </button>
                      ) : null}
                      <button
                        onClick={() => handleScrapeOne(acct.username)}
                        disabled={isScraping}
                        className="p-1.5 rounded-lg text-gray-400 hover:text-green-400 hover:bg-green-500/10 disabled:opacity-40 transition-colors"
                        title="Re-scrape"
                      >
                        <RefreshCw className={`w-4 h-4 ${isScraping ? 'animate-spin' : ''}`} />
                      </button>
                    </div>
                  </div>
                  {isExpanded && r?.links?.length ? (
                    <div className="px-5 pb-3 pt-1 bg-gray-900/40 border-t border-gray-700/30">
                      {r.bio && (
                        <p className="text-xs text-gray-400 italic mb-2 line-clamp-2">{r.bio}</p>
                      )}
                      <div className="flex flex-wrap gap-1.5">
                        {r.links.map((link, i) => (
                          <a
                            key={i}
                            href={link}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-sky-500/10 border border-sky-500/20 text-sky-400 hover:bg-sky-500/20 transition-colors max-w-xs truncate"
                          >
                            <ExternalLink className="w-3 h-3 shrink-0" />
                            {link.replace(/^https?:\/\//, '')}
                          </a>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}

// ── sub-components ─────────────────────────────────────────────────────────

function StatusCard({ label, value, color = 'text-white' }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-gray-800/40 border border-gray-700 rounded-xl p-4">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={`text-lg font-bold ${color}`}>{value}</p>
    </div>
  )
}

function AccountRow({
  account,
  onToggle,
  onDelete,
}: {
  account: XAccount
  onToggle: () => void
  onDelete: () => void
}) {
  return (
    <div className={`flex items-center gap-3 px-5 py-3 hover:bg-gray-800/30 transition-colors ${!account.active ? 'opacity-40' : ''}`}>
      <div className="w-9 h-9 rounded-full bg-sky-500/15 border border-sky-500/30 flex items-center justify-center shrink-0">
        <Twitter className="w-4 h-4 text-sky-400" />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-white">@{account.username}</span>
          <span className="text-xs text-gray-400">{account.display_name}</span>
          {account.builtin && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-gray-700 text-gray-400 border border-gray-600">built-in</span>
          )}
          <span className={`text-xs px-1.5 py-0.5 rounded border ${TIER_COLORS[account.tier]}`}>
            T{account.tier}
          </span>
        </div>
        <div className="flex items-center gap-1.5 mt-1 flex-wrap">
          {account.symbols.map(sym => (
            <span key={sym} className="text-xs px-1.5 py-0.5 rounded bg-gray-700/60 text-gray-300">
              {sym}
            </span>
          ))}
          <span className="text-xs text-gray-500">rel: {(account.reliability * 100).toFixed(0)}%</span>
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={onToggle}
          title={account.active ? 'Pause account' : 'Resume account'}
          className={`p-1.5 rounded-lg transition-colors ${
            account.active
              ? 'text-gray-400 hover:text-yellow-400 hover:bg-yellow-500/10'
              : 'text-gray-500 hover:text-green-400 hover:bg-green-500/10'
          }`}
        >
          {account.active ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
        </button>
        {!account.builtin && (
          <button
            onClick={onDelete}
            title="Remove account"
            className="p-1.5 rounded-lg text-gray-500 hover:text-red-400 hover:bg-red-500/10 transition-colors"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        )}
      </div>
    </div>
  )
}

// ── AuthSetupPanel ─────────────────────────────────────────────────────────

function CopyButton({ text, id, copied, onCopy }: { text: string; id: string; copied: string | null; onCopy: (t: string, k: string) => void }) {
  return (
    <button onClick={() => onCopy(text, id)} title="Copy" className="ml-2 p-0.5 rounded text-gray-500 hover:text-gray-300 transition-colors">
      {copied === id ? <CheckCircle2 className="w-3.5 h-3.5 text-green-400" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  )
}

function AuthSetupPanel({
  authMode,
  open,
  onToggle,
  copied,
  onCopy,
  onTestConnection,
  testing,
}: {
  authMode: 'oauth1' | 'bearer' | 'none'
  open: boolean
  onToggle: () => void
  copied: string | null
  onCopy: (text: string, key: string) => void
  onTestConnection: () => void
  testing: boolean
}) {
  const isOk = authMode !== 'none'

  const headerLabel =
    authMode === 'oauth1' ? 'OAuth 1.0a configured — verify Project enrollment below' :
    authMode === 'bearer' ? 'Bearer token configured ✓' :
    'Setup required — no Twitter credentials set'

  const headerColor =
    authMode === 'oauth1' ? 'text-amber-400' :
    authMode === 'bearer' ? 'text-green-400' :
    'text-red-400'

  const borderColor =
    authMode === 'bearer' ? 'border-gray-700' :
    authMode === 'oauth1' ? 'border-amber-500/40' :
    'border-red-500/40'

  const bgColor =
    authMode === 'bearer' ? 'bg-gray-800/40' :
    authMode === 'oauth1' ? 'bg-amber-500/5' :
    'bg-red-500/5'

  return (
    <section className={`rounded-xl border overflow-hidden transition-colors ${borderColor} ${bgColor}`}>
      <button onClick={onToggle} className="w-full flex items-center justify-between px-5 py-4 text-left">
        <span className={`text-sm font-semibold flex items-center gap-2 ${headerColor}`}>
          {authMode === 'bearer'
            ? <CheckCircle2 className="w-4 h-4 text-green-400" />
            : <AlertTriangle className="w-4 h-4" />}
          {headerLabel}
        </span>
        {open ? <ChevronUp className="w-4 h-4 text-gray-400 shrink-0" /> : <ChevronDown className="w-4 h-4 text-gray-400 shrink-0" />}
      </button>

      {open && (
        <div className="px-5 pb-6 space-y-5 border-t border-gray-700/50">

          {/* ── OAuth 1.0a path (creds are set, need Project enrollment) ── */}
          {authMode === 'oauth1' && (
            <>
              <div className="pt-4 p-4 rounded-lg bg-amber-500/10 border border-amber-500/30 space-y-3">
                <p className="text-sm font-semibold text-amber-300 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4" />
                  Your app is not yet enrolled in a Twitter Developer Project
                </p>
                <p className="text-sm text-gray-300">
                  Your OAuth 1.0a credentials are saved. Twitter&apos;s free tier requires your app to live inside
                  a <strong className="text-white">Project</strong> before it can read timelines.
                  This is a one-time 2-minute fix on the Developer Portal — no payment needed.
                </p>
                <button
                  onClick={onTestConnection}
                  disabled={testing}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-300 text-sm font-medium transition-colors disabled:opacity-40"
                >
                  <Activity className={`w-4 h-4 ${testing ? 'animate-pulse' : ''}`} />
                  {testing ? 'Testing…' : 'Test connection first (might already work)'}
                </button>
              </div>

              <ol className="space-y-5 text-sm pt-1">
                <Step num={1} title="Open the Developer Portal projects page">
                  <a
                    href="https://developer.twitter.com/en/portal/projects-and-apps"
                    target="_blank" rel="noopener noreferrer"
                    className="inline-flex items-center gap-1.5 mt-1.5 px-3 py-1.5 rounded-lg bg-sky-500/15 border border-sky-500/30 text-sky-400 hover:bg-sky-500/25 text-xs font-medium transition-colors"
                  >
                    <Twitter className="w-3.5 h-3.5" />
                    developer.twitter.com → Projects &amp; Apps
                    <ExternalLink className="w-3 h-3" />
                  </a>
                </Step>

                <Step num={2} title='Click "+ Create Project"'>
                  <p className="text-gray-400">Give it any name (e.g. <em>TradeBot</em>), pick <em>Making a bot</em> as use case.</p>
                </Step>

                <Step num={3} title="Attach your existing app">
                  <p className="text-gray-400">
                    When prompted, choose <strong className="text-white">Add an existing App</strong> and select
                    your app (the one whose API Key starts with{' '}
                    <code className="bg-gray-800 px-1 rounded text-gray-300">M27KPzZx…</code>).
                  </p>
                  <p className="text-xs text-gray-500 mt-1">
                    If you don&apos;t see your app listed, go to <em>Standalone Apps</em> and move it into the project.
                  </p>
                </Step>

                <Step num={4} title="Set App permissions to Read">
                  <p className="text-gray-400">
                    Inside the App → <strong className="text-white">Settings → User authentication settings</strong>.
                    Set <strong className="text-white">App permissions</strong> to <strong className="text-white">Read</strong>.
                  </p>
                </Step>

                <Step num={5} title="Regenerate your Access Token &amp; Secret">
                  <p className="text-gray-400">
                    Go to <strong className="text-white">Keys and Tokens</strong> and click{' '}
                    <strong className="text-white">Regenerate</strong> next to{' '}
                    <em>Access Token and Secret</em> (permissions only take effect on new tokens).
                  </p>
                  <div className="mt-2 space-y-1.5">
                    {[
                      { label: 'TWITTER_ACCESS_TOKEN=', id: 'k1' },
                      { label: 'TWITTER_ACCESS_TOKEN_SECRET=', id: 'k2' },
                    ].map(({ label, id }) => (
                      <div key={id} className="flex items-center gap-2 p-2 rounded-lg bg-gray-900 border border-gray-700">
                        <code className="text-xs font-mono text-green-400 flex-1">{label}<span className="text-gray-500">new_value_here</span></code>
                      </div>
                    ))}
                  </div>
                  <p className="text-xs text-gray-500 mt-1.5">
                    Update <code className="bg-gray-800 px-1 rounded">.env</code> then restart the backend, then click <em>Test connection</em> above.
                  </p>
                </Step>

                <Step num={6} title="Follow all 19 monitored accounts from @soxentp">
                  <p className="text-gray-400 text-xs">
                    The monitor reads your home timeline — accounts you don&apos;t follow won&apos;t appear.
                    Tap each to open on X/Twitter and follow:
                  </p>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {[
                      'realDonaldTrump','elonmusk','saylor','GaryGensler','SECGov',
                      'federalreserve','POTUS','VitalikButerin','cz_binance','brian_armstrong',
                      'CathieDWood','novogratz','tyler','APompliano','RayDalio',
                      'GoldmanSachs','jpmorgan','PeterSchiff','jimcramer',
                    ].map(u => (
                      <a
                        key={u}
                        href={`https://x.com/${u}`}
                        target="_blank" rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 px-2 py-1 rounded bg-sky-500/10 border border-sky-500/20 text-sky-400 text-xs hover:bg-sky-500/20 transition-colors"
                      >
                        @{u}
                        <ExternalLink className="w-2.5 h-2.5" />
                      </a>
                    ))}
                  </div>
                </Step>
              </ol>
            </>
          )}

          {/* ── Bearer token path ── */}
          {authMode === 'bearer' && (
            <div className="pt-4 p-4 rounded-lg bg-green-500/10 border border-green-500/30">
              <p className="text-sm text-green-300 flex items-center gap-2">
                <CheckCircle2 className="w-4 h-4" />
                Bearer token is set. Use <strong className="text-white">Test connection</strong> to verify read access.
              </p>
              <p className="text-xs text-gray-400 mt-2">
                Bearer token requires <strong className="text-white">Basic tier ($100/mo)</strong> for timeline reads.
                Add OAuth 1.0a keys to use the free-tier home-timeline path instead.
              </p>
            </div>
          )}

          {/* ── No creds ── */}
          {authMode === 'none' && (
            <ol className="space-y-4 text-sm pt-4">
              <Step num={1} title="Go to the Twitter Developer Portal">
                <a href="https://developer.twitter.com/en/portal/dashboard" target="_blank" rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 mt-1.5 px-3 py-1.5 rounded-lg bg-sky-500/15 border border-sky-500/30 text-sky-400 hover:bg-sky-500/25 text-xs font-medium transition-colors">
                  <Twitter className="w-3.5 h-3.5" />Open Twitter Developer Portal<ExternalLink className="w-3 h-3" />
                </a>
              </Step>
              <Step num={2} title="Create a Project + App">
                <p className="text-gray-400">Click <strong className="text-white">+ Create Project</strong>, attach or create an App with <strong className="text-white">Read</strong> permissions.</p>
              </Step>
              <Step num={3} title="Copy all four keys from Keys and Tokens">
                <div className="mt-2 space-y-1">
                  {['TWITTER_API_KEY=','TWITTER_API_SECRET=','TWITTER_ACCESS_TOKEN=','TWITTER_ACCESS_TOKEN_SECRET=','TWITTER_USER_ID='].map(k => (
                    <div key={k} className="flex items-center gap-2 p-2 rounded-lg bg-gray-900 border border-gray-700">
                      <code className="text-xs font-mono text-green-400 flex-1">{k}<span className="text-gray-500">value_here</span></code>
                      <CopyButton text={k} id={k} copied={copied} onCopy={onCopy} />
                    </div>
                  ))}
                </div>
                <p className="text-xs text-gray-500 mt-1.5">Paste into your <code className="bg-gray-800 px-1 rounded">.env</code> and restart the backend.</p>
              </Step>
            </ol>
          )}

        </div>
      )}
    </section>
  )
}

function Step({ num, title, children }: { num: number; title: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-4">
      <span className="flex-none w-6 h-6 rounded-full bg-sky-500/20 border border-sky-500/30 text-sky-400 text-xs font-bold flex items-center justify-center mt-0.5">
        {num}
      </span>
      <div className="flex-1 space-y-1">
        <p className="font-semibold text-white">{title}</p>
        {children}
      </div>
    </li>
  )
}

// ──────────────────────────────────────────────────────────────────────────────

function TweetCard({ tweet }: { tweet: XTweet }) {  const [expanded, setExpanded] = useState(false)
  const text = tweet.text ?? ''
  const isLong = text.length > 200
  const displayText = isLong && !expanded ? text.slice(0, 200) + '…' : text

  const hasBullish = /\b(buy|long|bull|pump|moon|up|surge|breakout|ath|rally|accumulate)\b/i.test(text)
  const hasBearish = /\b(sell|short|bear|dump|crash|down|drop|correction|rekt|fall)\b/i.test(text)
  const sentiment = hasBullish && !hasBearish ? 'bullish' : hasBearish && !hasBullish ? 'bearish' : 'neutral'

  const borderColor =
    sentiment === 'bullish' ? 'border-green-500/20' :
    sentiment === 'bearish' ? 'border-red-500/20' :
    'border-gray-700/30'

  return (
    <div className={`px-5 py-3.5 border-l-2 ${borderColor}`}>
      <div className="flex items-start gap-3">
        <div className="w-8 h-8 rounded-full bg-sky-500/15 border border-sky-500/20 flex items-center justify-center shrink-0">
          <Twitter className="w-3.5 h-3.5 text-sky-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className="text-sm font-medium text-white">@{tweet.username}</span>
            <span className="text-xs text-gray-500">{tweet.display}</span>
            <span className={`text-xs px-1.5 py-0.5 rounded border ${TIER_COLORS[tweet.tier] ?? 'text-gray-400 bg-gray-700 border-gray-600'}`}>
              T{tweet.tier}
            </span>
            {sentiment !== 'neutral' && (
              <span className={`text-xs font-medium ${sentiment === 'bullish' ? 'text-green-400' : 'text-red-400'}`}>
                {sentiment === 'bullish' ? '↑ Bullish' : '↓ Bearish'}
              </span>
            )}
            <span className="text-xs text-gray-600 ml-auto">
              {tweet.fetched_at ? new Date(tweet.fetched_at).toLocaleTimeString() : ''}
            </span>
          </div>

          <p className="text-sm text-gray-300 whitespace-pre-wrap break-words leading-relaxed">
            {displayText}
          </p>

          {isLong && (
            <button
              onClick={() => setExpanded(v => !v)}
              className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-300 mt-1 transition-colors"
            >
              {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              {expanded ? 'Show less' : 'Show more'}
            </button>
          )}

          {tweet.symbols.length > 0 && (
            <div className="flex gap-1 mt-2 flex-wrap">
              {tweet.symbols.map(sym => (
                <span key={sym} className="text-xs px-1.5 py-0.5 rounded bg-gray-700 text-gray-300 border border-gray-600">
                  {sym}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
