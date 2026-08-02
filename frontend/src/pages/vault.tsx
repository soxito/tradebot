/**
 * Vault — Obsidian Knowledge Browser
 *
 * Displays notes synced from the Obsidian vault:
 *  - Status panel (vault path, file count, last sync, REST connection)
 *  - Full-text search
 *  - Filterable note list (signal / decision / strategy / community / daily)
 *  - Markdown note detail panel
 *  - Trigger sync button
 */
import Head from 'next/head'
import Link from 'next/link'
import { useCallback, useEffect, useState, useRef } from 'react'
import { apiClient } from '@/services/api'
import {
  BookOpen, Brain, Check, ChevronRight, Clock, ExternalLink, FileText,
  Filter, Link2, RefreshCw, Search, Tag, Wifi, WifiOff, X, Zap,
  FolderOpen, Users, TrendingUp, Activity, Database, Network, MessageSquareText, Monitor,
} from 'lucide-react'

/** "3m ago" / "in 4m" — a sync's recency is easier to judge than a timestamp. */
function relativeTime(iso: string): string {
  const deltaMs = new Date(iso).getTime() - Date.now()
  const future = deltaMs > 0
  const mins = Math.round(Math.abs(deltaMs) / 60_000)
  if (mins < 1) return future ? 'in <1m' : 'just now'
  const body = mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${mins % 60}m`
  return future ? `in ${body}` : `${body} ago`
}

// ─── VaultLiveFeed — compact live action strip for the vault page ─────────────
function VaultLiveFeed() {
  const [feed, setFeed]   = useState<any[]>([])
  const [pulse, setPulse] = useState(false)
  const prevLen           = useRef(0)

  useEffect(() => {
    const load = () => {
      apiClient.obsidian.liveFeed(10)
        .then(r => {
          const items = r.data?.feed ?? []
          if (items.length > prevLen.current) {
            setPulse(true)
            setTimeout(() => setPulse(false), 2000)
          }
          prevLen.current = items.length
          setFeed(items)
        })
        .catch(() => {})
    }
    load()
    const t = setInterval(load, 10_000)
    return () => clearInterval(t)
  }, [])

  const ACTION_ICONS: Record<string,string> = {
    'jarvis-set_tp': '🎯', 'jarvis-set_sl': '🛡️', 'jarvis-close': '❌',
    'agent-decision': '🧠', 'decision-outcome': '📊', 'jarvis-learning': '💡',
  }
  const timeAgo = (iso: string) => {
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000)
    return mins < 1 ? 'now' : mins < 60 ? `${mins}m` : `${Math.floor(mins/60)}h`
  }

  if (!feed.length) return null
  return (
    <div className={`bg-gray-900/40 border ${pulse ? 'border-cyan-500/30' : 'border-gray-700/30'} rounded-lg p-3 transition-colors`}>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-semibold text-gray-300 flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${pulse ? 'bg-cyan-400 animate-ping' : 'bg-emerald-500 animate-pulse'}`} />
          Live Brain Activity
        </h3>
        <Link href="/intelligence" className="text-[10px] text-violet-400 hover:text-violet-300 flex items-center gap-0.5">
          <Network className="w-3 h-3" /> brain map
        </Link>
      </div>
      <div className="flex gap-3 flex-wrap">
        {feed.slice(0, 8).map((item, i) => (
          <div key={item.id ?? i} className="flex items-center gap-1 text-[10px]">
            <span>{ACTION_ICONS[item.type] || '⚡'}</span>
            <span className="text-gray-400">
              {item.type.replace('jarvis-','').replace('-',' ')}
              {item.symbol ? ` · ${item.symbol}` : ''}
            </span>
            <span className="text-gray-700">{timeAgo(item.timestamp)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Types ────────────────────────────────────────────────────────────────────

interface VaultStatus {
  vault_path: string
  vault_exists: boolean
  total_notes: number
  notes_by_type: Record<string, number>
  last_sync_at: string | null
  obsidian_rest_connected: boolean
  obsidian_rest_url: string
}

/** The auto-sync loop's own record of when a sync actually ran.
 *  `VaultStatus.last_sync_at` is only the newest note's timestamp, so a cycle
 *  that finds nothing to write leaves it drifting ever older. */
interface VaultSyncStatus {
  running: boolean
  interval_seconds: number
  started_at: string | null
  next_run_at: string | null
  last_run: {
    at: string
    status: 'ok' | 'partial' | 'error'
    written?: number
    skipped?: number
    errors?: number
    duration_ms?: number
    trigger?: 'auto' | 'manual'
    error?: string
  } | null
}

interface VaultNote {
  id: number
  path: string
  note_type: string
  symbol: string | null
  tags: string[]
  created_at: string
  updated_at: string
  synced_to_obsidian: boolean
}

interface NoteContent {
  path: string
  content: string
  note_type: string
  symbol: string | null
  tags: string[]
  frontmatter: Record<string, string>
}

interface SearchHit {
  path: string
  note_type: string
  symbol: string | null
  excerpt: string
  score: number
}

// ─── Constants ────────────────────────────────────────────────────────────────

const NOTE_TYPE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  signal:    Zap,
  decision:  Brain,
  strategy:  TrendingUp,
  community: Users,
  daily:     Clock,
  trade:     Activity,
  custom:    FileText,
}

const NOTE_TYPE_COLORS: Record<string, string> = {
  signal:    'text-amber-400 bg-amber-400/10 border-amber-400/20',
  decision:  'text-violet-400 bg-violet-400/10 border-violet-400/20',
  strategy:  'text-emerald-400 bg-emerald-400/10 border-emerald-400/20',
  community: 'text-blue-400 bg-blue-400/10 border-blue-400/20',
  daily:     'text-cyan-400 bg-cyan-400/10 border-cyan-400/20',
  trade:     'text-green-400 bg-green-400/10 border-green-400/20',
  custom:    'text-gray-400 bg-gray-400/10 border-gray-400/20',
}

function noteColor(type: string) {
  return NOTE_TYPE_COLORS[type] || NOTE_TYPE_COLORS.custom
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function NoteTypeBadge({ type }: { type: string }) {
  const Icon = NOTE_TYPE_ICONS[type] || FileText
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium border ${noteColor(type)}`}>
      <Icon className="w-3 h-3" />
      {type}
    </span>
  )
}

function StatusBadge({ connected, restConfigured }: { connected: boolean; restConfigured: boolean }) {
  if (connected) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-emerald-400">
        <Wifi className="w-3 h-3" /> Obsidian live sync active
      </span>
    )
  }
  if (restConfigured) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-amber-500">
        <WifiOff className="w-3 h-3" /> Obsidian not running (REST configured)
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs text-gray-500">
      <FileText className="w-3 h-3" /> File sync only · <a href="/vault#setup" className="underline hover:text-gray-300">connect Obsidian</a>
    </span>
  )
}

function MarkdownPreview({ content }: { content: string }) {
  // Simple renderer: strip frontmatter, render code blocks, headings, bullets
  const body = content.replace(/^---[\s\S]*?---\n?/, '').trim()
  return (
    <pre className="whitespace-pre-wrap text-xs text-gray-300 font-mono leading-relaxed">
      {body}
    </pre>
  )
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function VaultPage() {
  const [status, setStatus]           = useState<VaultStatus | null>(null)
  const [notes, setNotes]             = useState<VaultNote[]>([])
  const [selectedNote, setSelectedNote] = useState<NoteContent | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchHits, setSearchHits]   = useState<SearchHit[]>([])
  const [filterType, setFilterType]   = useState<string>('')
  const [filterSymbol, setFilterSymbol] = useState<string>('')
  const [syncing, setSyncing]         = useState(false)
  const [syncResult, setSyncResult]   = useState<string | null>(null)
  const [syncStatus, setSyncStatus]   = useState<VaultSyncStatus | null>(null)
  const [loading, setLoading]         = useState(true)

  // ── Data fetching ──────────────────────────────────────────────────────────

  const fetchStatus = useCallback(async () => {
    try {
      const { data } = await apiClient.obsidian.status()
      setStatus(data)
    } catch { /* ignore */ }
    try {
      const { data } = await apiClient.obsidian.syncStatus()
      setSyncStatus(data)
    } catch { /* the loop is optional — the page still works without it */ }
  }, [])

  const fetchNotes = useCallback(async () => {
    try {
      const { data } = await apiClient.obsidian.listNotes({
        note_type: filterType || undefined,
        symbol: filterSymbol || undefined,
        limit: 100,
      })
      setNotes(data.notes || [])
    } catch { /* ignore */ }
    setLoading(false)
  }, [filterType, filterSymbol])

  useEffect(() => {
    fetchStatus()
    fetchNotes()
  }, [fetchStatus, fetchNotes])

  // ── Search ─────────────────────────────────────────────────────────────────

  const handleSearch = useCallback(async () => {
    if (!searchQuery.trim()) {
      setSearchHits([])
      return
    }
    try {
      const { data } = await apiClient.obsidian.search({
        query: searchQuery,
        note_type: filterType || undefined,
        symbol: filterSymbol || undefined,
        limit: 30,
      })
      setSearchHits(data.hits || [])
    } catch { /* ignore */ }
  }, [searchQuery, filterType, filterSymbol])

  // ── Note detail ────────────────────────────────────────────────────────────

  const openNote = useCallback(async (path: string) => {
    try {
      const { data } = await apiClient.obsidian.getNote(path)
      setSelectedNote(data)
    } catch { /* ignore */ }
  }, [])

  const openInObsidian = useCallback(async (path: string) => {
    try {
      await apiClient.obsidian.openInObsidian(path)
    } catch { /* ignore */ }
  }, [])

  // Keep the auto-sync panel current while the page sits open — a stale
  // "last sync: 40m ago" on a loop that runs every 5 minutes is worse than none.
  useEffect(() => {
    const t = setInterval(fetchStatus, 30_000)
    return () => clearInterval(t)
  }, [fetchStatus])

  // ── Sync ───────────────────────────────────────────────────────────────────

  const handleSync = useCallback(async () => {
    setSyncing(true)
    setSyncResult(null)
    try {
      const { data } = await apiClient.obsidian.sync({
        export_decisions: true,
        export_signals: true,
        export_communities: true,
        limit: 100,
      })
      const r = data.result
      setSyncResult(`✓ Written: ${r.written}  Skipped: ${r.skipped}  Errors: ${r.errors}  (${r.duration_ms}ms)`)
      await fetchStatus()
      await fetchNotes()
    } catch (e: any) {
      setSyncResult(`✗ Sync failed: ${e?.response?.data?.detail || e.message}`)
    }
    setSyncing(false)
  }, [fetchStatus, fetchNotes])

  // ── Display list ───────────────────────────────────────────────────────────

  const displayNotes = searchHits.length > 0
    ? searchHits.map(h => ({
        id: 0, path: h.path, note_type: h.note_type, symbol: h.symbol,
        tags: [], created_at: '', updated_at: '', synced_to_obsidian: false,
        _excerpt: h.excerpt, _score: h.score,
      }))
    : notes.map(n => ({ ...n, _excerpt: undefined, _score: undefined }))

  const NOTE_TYPES = ['signal', 'decision', 'strategy', 'community', 'daily', 'trade', 'jarvis-learning']

  return (
    <>
      <Head><title>Obsidian Vault — TradeBot</title></Head>

      <div className="p-4 space-y-4">

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <BookOpen className="w-6 h-6 text-violet-400" />
            <div>
              <h1 className="text-xl font-bold text-white">Obsidian Knowledge Vault</h1>
              <p className="text-xs text-gray-500 flex items-center gap-2 flex-wrap">
                <span className="font-mono text-gray-600">{status?.vault_path ?? 'Loading…'}</span>
                <StatusBadge
                  connected={status?.obsidian_rest_connected ?? false}
                  restConfigured={Boolean(status?.obsidian_rest_url && !status.obsidian_rest_url.startsWith('(not'))}
                />
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {/* Open vault in Obsidian — uses the obsidian:// URL scheme (macOS/Windows/Linux) */}
            <a
              href={`obsidian://open?vault=${encodeURIComponent(status?.vault_path?.split('/').pop() ?? 'tradebot')}`}
              className="flex items-center gap-2 px-3 py-2 bg-gray-800 hover:bg-gray-700 border border-gray-600/50 text-gray-300 hover:text-white rounded-lg text-sm font-medium transition-colors"
              title="Open this vault in the Obsidian app"
            >
              <ExternalLink className="w-4 h-4" /> Open in Obsidian
            </a>
            <button
              onClick={handleSync}
              disabled={syncing}
              className="flex items-center gap-2 px-4 py-2 bg-violet-600 hover:bg-violet-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
            >
              <RefreshCw className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />
              {syncing ? 'Syncing…' : 'Sync Now'}
            </button>
          </div>
        </div>

        {/* ── Auto-sync state ─────────────────────────────────────────────
            The vault is only worth anything if it is current, so when it last
            synced is a first-class fact on this page rather than something you
            infer from note timestamps. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2 bg-gray-800/40 border border-gray-700/40 rounded-lg text-xs">
          <span className="flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${
              syncStatus?.running ? 'bg-emerald-400 animate-pulse' : 'bg-gray-500'
            }`} />
            <span className={syncStatus?.running ? 'text-emerald-300' : 'text-gray-400'}>
              {syncStatus?.running
                ? `Auto-sync every ${Math.round((syncStatus.interval_seconds || 300) / 60)} min`
                : 'Auto-sync off'}
            </span>
          </span>

          <span className="text-gray-500">
            Last sync:{' '}
            <span className={
              syncStatus?.last_run?.status === 'error' ? 'text-red-400'
              : syncStatus?.last_run ? 'text-gray-200' : 'text-gray-600'
            }>
              {syncStatus?.last_run ? relativeTime(syncStatus.last_run.at) : 'not yet this session'}
            </span>
            {syncStatus?.last_run?.trigger === 'manual' && (
              <span className="text-gray-600"> (manual)</span>
            )}
          </span>

          {syncStatus?.last_run && syncStatus.last_run.status !== 'error' && (
            <span className="text-gray-600">
              {syncStatus.last_run.written ?? 0} written · {syncStatus.last_run.skipped ?? 0} unchanged
              {(syncStatus.last_run.errors ?? 0) > 0 && (
                <span className="text-amber-400"> · {syncStatus.last_run.errors} error(s)</span>
              )}
              {syncStatus.last_run.duration_ms != null && ` · ${syncStatus.last_run.duration_ms}ms`}
            </span>
          )}

          {syncStatus?.last_run?.status === 'error' && (
            <span className="text-red-400 truncate max-w-md">{syncStatus.last_run.error}</span>
          )}

          {syncStatus?.running && syncStatus.next_run_at && (
            <span className="text-gray-600 ml-auto">next {relativeTime(syncStatus.next_run_at)}</span>
          )}
        </div>

        {syncResult && (
          <div className={`text-xs px-3 py-2 rounded-lg border ${
            syncResult.startsWith('✓')
              ? 'bg-emerald-900/20 border-emerald-500/30 text-emerald-400'
              : 'bg-red-900/20 border-red-500/30 text-red-400'
          }`}>
            {syncResult}
          </div>
        )}

        {/* ── Live brain feed strip ───────────────────────────────────────── */}
        <VaultLiveFeed />

        {/* ── Cross-brain navigation bar ─────────────────────────────────── */}
        <div className="flex items-center gap-2 flex-wrap text-xs">
          <span className="text-gray-600 text-xs">Connected brains:</span>
          <Link href="/intelligence"
            className="flex items-center gap-1.5 px-2.5 py-1 bg-gray-800/50 border border-gray-700/40 rounded-lg text-gray-300 hover:text-white hover:border-violet-500/40 transition-colors">
            <Network className="w-3.5 h-3.5 text-violet-400" /> Intelligence Brain
          </Link>
          <Link href="/telegram-signals"
            className="flex items-center gap-1.5 px-2.5 py-1 bg-gray-800/50 border border-gray-700/40 rounded-lg text-gray-300 hover:text-white hover:border-blue-500/40 transition-colors">
            <MessageSquareText className="w-3.5 h-3.5 text-blue-400" /> Telegram Signals
          </Link>
          <Link href="/mt5-live"
            className="flex items-center gap-1.5 px-2.5 py-1 bg-gray-800/50 border border-gray-700/40 rounded-lg text-gray-300 hover:text-white hover:border-green-500/40 transition-colors">
            <Monitor className="w-3.5 h-3.5 text-green-400" /> MT5 Live
          </Link>
          {(status?.notes_by_type['jarvis-learning'] ?? 0) > 0 && (
            <button
              onClick={() => setFilterType('jarvis-learning')}
              className="flex items-center gap-1.5 px-2.5 py-1 bg-violet-900/20 border border-violet-500/30 rounded-lg text-violet-300 hover:text-violet-200 transition-colors">
              <Brain className="w-3.5 h-3.5" />
              {status?.notes_by_type['jarvis-learning']} Jarvis Learnings
            </button>
          )}
        </div>

        {/* ── Stats bar ──────────────────────────────────────────────────── */}
        {status && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              { label: 'Total Notes', value: status.total_notes, icon: FileText },
              { label: 'Signals', value: status.notes_by_type.signal ?? 0, icon: Zap },
              { label: 'Decisions', value: status.notes_by_type.decision ?? 0, icon: Brain },
              { label: 'Communities', value: status.notes_by_type.community ?? 0, icon: Users },
            ].map(({ label, value, icon: Icon }) => (
              <div key={label} className="bg-gray-800/50 border border-gray-700/50 rounded-lg p-3">
                <div className="flex items-center gap-2 text-gray-400 text-xs mb-1">
                  <Icon className="w-3.5 h-3.5" />
                  {label}
                </div>
                <div className="text-2xl font-bold text-white">{value.toLocaleString()}</div>
              </div>
            ))}
          </div>
        )}

        {/* ── Search + filters ───────────────────────────────────────────── */}
        <div className="flex gap-2 flex-wrap">
          <div className="flex-1 min-w-48 flex items-center gap-2 bg-gray-800/50 border border-gray-700/50 rounded-lg px-3">
            <Search className="w-4 h-4 text-gray-500 shrink-0" />
            <input
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              placeholder="Search vault notes…"
              className="w-full bg-transparent text-sm text-gray-200 placeholder-gray-600 py-2 outline-none"
            />
            {searchQuery && (
              <button onClick={() => { setSearchQuery(''); setSearchHits([]) }}>
                <X className="w-4 h-4 text-gray-500 hover:text-gray-300" />
              </button>
            )}
          </div>

          <select
            value={filterType}
            onChange={e => setFilterType(e.target.value)}
            className="bg-gray-800/50 border border-gray-700/50 rounded-lg px-3 py-2 text-sm text-gray-300 outline-none"
          >
            <option value="">All types</option>
            {NOTE_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>

          <button
            onClick={handleSearch}
            className="px-3 py-2 bg-blue-600/20 border border-blue-500/30 rounded-lg text-sm text-blue-400 hover:bg-blue-600/30 transition-colors"
          >
            Search
          </button>
        </div>

        {/* ── Main content (list + detail) ───────────────────────────────── */}
        <div className="flex gap-4" style={{ minHeight: '60vh' }}>

          {/* Note list */}
          <div className="w-80 shrink-0 flex flex-col gap-1 overflow-y-auto max-h-[70vh] pr-1">
            {loading && (
              <div className="text-center text-gray-600 text-sm py-8">Loading notes…</div>
            )}
            {!loading && displayNotes.length === 0 && (
              <div className="text-center py-8 space-y-2">
                <BookOpen className="w-8 h-8 text-gray-700 mx-auto" />
                <p className="text-gray-600 text-sm">No notes yet. Click Sync Now to populate the vault.</p>
              </div>
            )}
            {displayNotes.map((note, i) => {
              const Icon = NOTE_TYPE_ICONS[note.note_type] || FileText
              const isSelected = selectedNote?.path === note.path
              return (
                <button
                  key={note.path + i}
                  onClick={() => openNote(note.path)}
                  className={`w-full text-left p-3 rounded-lg border transition-all ${
                    isSelected
                      ? 'bg-violet-900/20 border-violet-500/40'
                      : 'bg-gray-800/40 border-gray-700/40 hover:border-gray-600/60'
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <Icon className="w-4 h-4 mt-0.5 shrink-0 text-gray-500" />
                    <div className="flex-1 min-w-0">
                      <div className="text-xs font-medium text-gray-200 truncate">
                        {note.path.split('/').pop()?.replace('.md', '') ?? note.path}
                      </div>
                      <div className="flex items-center gap-2 mt-1">
                        <NoteTypeBadge type={note.note_type} />
                        {note.symbol && (
                          <span className="text-xs text-gray-500">{note.symbol}</span>
                        )}
                      </div>
                      {(note as any)._excerpt && (
                        <p className="text-xs text-gray-600 mt-1 line-clamp-2">
                          {(note as any)._excerpt}
                        </p>
                      )}
                    </div>
                    <ChevronRight className="w-3 h-3 text-gray-600 shrink-0 mt-1" />
                  </div>
                </button>
              )
            })}
          </div>

          {/* Detail panel */}
          <div className="flex-1 bg-gray-800/40 border border-gray-700/50 rounded-xl overflow-hidden flex flex-col">
            {selectedNote ? (
              <>
                <div className="flex items-center justify-between p-4 border-b border-gray-700/50">
                  <div className="space-y-1">
                    <h3 className="text-sm font-semibold text-white">
                      {selectedNote.path.split('/').pop()?.replace('.md', '')}
                    </h3>
                    <div className="flex items-center gap-2">
                      <NoteTypeBadge type={selectedNote.note_type} />
                      {selectedNote.symbol && (
                        <span className="text-xs text-gray-500">{selectedNote.symbol}</span>
                      )}
                      {selectedNote.tags.slice(0, 3).map(tag => (
                        <span key={tag} className="text-xs text-gray-600 bg-gray-700/40 rounded px-1.5 py-0.5">
                          #{tag}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => openInObsidian(selectedNote.path)}
                      className="flex items-center gap-1 text-xs text-violet-400 hover:text-violet-300 border border-violet-500/30 hover:border-violet-400/50 px-2 py-1 rounded transition-colors"
                      title="Open in Obsidian"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Open in Obsidian
                    </button>
                    <button onClick={() => setSelectedNote(null)}>
                      <X className="w-4 h-4 text-gray-500 hover:text-gray-300" />
                    </button>
                  </div>
                </div>
                <div className="flex-1 overflow-y-auto p-4">
                  <MarkdownPreview content={selectedNote.content} />
                </div>
              </>
            ) : (
              <div className="flex-1 flex items-center justify-center text-center p-8">
                <div className="space-y-3">
                  <BookOpen className="w-12 h-12 text-gray-700 mx-auto" />
                  <p className="text-gray-500 text-sm">Select a note to preview its content</p>
                  <p className="text-gray-700 text-xs">
                    {status?.total_notes === 0
                      ? 'Run "Sync Now" to populate the vault from agent decisions and signals.'
                      : `${status?.total_notes} notes available`}
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ── Setup panel (collapsed by default, shown when REST not configured) ── */}
        {status && !status.obsidian_rest_connected && (
          <div id="setup" className="bg-gray-800/30 border border-gray-700/30 rounded-xl p-4 text-xs text-gray-400 space-y-3">
            <h3 className="text-sm font-semibold text-gray-300 flex items-center gap-2">
              <Link2 className="w-4 h-4 text-violet-400" /> Connect Obsidian for Live Sync
            </h3>
            <div className="grid sm:grid-cols-3 gap-3">
              {[
                { step: '1', title: 'Open vault in Obsidian', desc: 'Click "Open in Obsidian" above or open Obsidian → Open folder as vault → select the vault path shown above.' },
                { step: '2', title: 'Install Local REST API plugin', desc: 'Settings → Community plugins → search "Local REST API" → Install → Enable. Copy the API token.' },
                { step: '3', title: 'Add token to .env', desc: 'Add OBSIDIAN_REST_URL=https://localhost:27124 and OBSIDIAN_REST_TOKEN=<token> to backend/.env, then restart.' },
              ].map(({ step, title, desc }) => (
                <div key={step} className="flex gap-3">
                  <span className="w-6 h-6 rounded-full bg-violet-600/30 text-violet-400 flex items-center justify-center font-bold text-xs shrink-0">{step}</span>
                  <div>
                    <div className="font-medium text-gray-300 mb-1">{title}</div>
                    <div className="text-gray-500">{desc}</div>
                  </div>
                </div>
              ))}
            </div>
            <p className="text-gray-600">
              Without live sync the vault still works — notes are written to disk on every "Sync Now" and can be opened directly in Obsidian.
            </p>
          </div>
        )}
      </div>
    </>
  )
}
