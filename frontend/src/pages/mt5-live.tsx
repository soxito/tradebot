/**
 * /mt5-live — Full MT5 account management + live chart with position overlays
 *
 * Layout:
 *  Top:    Account tabs (LIVE / DEMO / PROP badge) + Sync + Add Account
 *  Mid-L:  Account metrics cards (balance, equity, free margin, floating P&L)
 *  Mid-R:  Open Positions list (click to focus chart symbol)
 *  Chart:  MT5 real-time candlestick chart with SL/TP/entry overlays
 *  Right:  Pending orders + Risk summary
 */
import Head from 'next/head'
import { useState, useEffect, useCallback, useRef } from 'react'
import { pollMultiplier } from '@/utils/devicePerformance'
import dynamic from 'next/dynamic'
import { apiClient } from '@/services/api'
import type { MT5PositionForChart, MT5DealForChart } from '@/components/MT5Chart'
import ChartErrorBoundary from '@/components/ChartErrorBoundary'
import MT5AccountBadge from '@/components/MT5AccountBadge'

// Heavy chart components — lazy loaded so they don't block the initial render.
// Each chart weighs ~300-500 kB parsed JS (lightweight-charts + TA logic).
const MT5Chart = dynamic(() => import('@/components/MT5Chart'), {
  ssr: false,
  loading: () => <div className="flex items-center justify-center h-96 bg-gray-800/50 rounded-xl text-gray-500 text-sm">Loading chart…</div>,
})
const MT5AdvancedChart = dynamic(() => import('@/components/MT5AdvancedChart'), {
  ssr: false,
  loading: () => <div className="flex items-center justify-center h-96 bg-gray-800/50 rounded-xl text-gray-500 text-sm">Loading chart…</div>,
})
const MT5SniperChart = dynamic(() => import('@/components/MT5SniperChart'), {
  ssr: false,
  loading: () => <div className="flex items-center justify-center h-96 bg-gray-800/50 rounded-xl text-gray-500 text-sm">Loading chart…</div>,
})
const MT5ScalpBotPanel = dynamic(() => import('@/components/MT5ScalpBotPanel'), {
  ssr: false,
  loading: () => <div className="flex items-center justify-center h-48 bg-gray-800/50 rounded-xl text-gray-500 text-sm">Loading scalpbot…</div>,
})
import { formatDateTimeCompactZA, formatTimeZA } from '@/utils/datetime'
import {
  Monitor, Plus, Trash2, RefreshCw, AlertTriangle, CheckCircle,
  Loader2, DollarSign, TrendingUp, TrendingDown, Shield,
  BarChart2, ArrowUpDown, X, Search, Pencil, ChevronLeft, ChevronRight, Brain,
} from 'lucide-react'

// ── Known broker server list ───────────────────────────────────────────────────
// Format: { label (broker display name), servers: [server strings] }
// Users can still type any custom server manually.

const BROKER_SERVERS: { broker: string; servers: string[] }[] = [
  // IC Markets
  { broker: 'IC Markets', servers: [
    'ICMarkets-Live01','ICMarkets-Live02','ICMarkets-Live03','ICMarkets-Live04',
    'ICMarkets-Live05','ICMarkets-Live06','ICMarkets-Live07',
    'ICMarkets-Demo01','ICMarkets-Demo02','ICMarkets-Demo03',
  ]},
  // Pepperstone
  { broker: 'Pepperstone', servers: [
    'Pepperstone-Live','Pepperstone-Live01','Pepperstone-Live02',
    'Pepperstone-Live03','Pepperstone-Edge-Live',
    'Pepperstone-Demo','Pepperstone-Demo01',
  ]},
  // XM
  { broker: 'XM', servers: [
    'XMGlobal-Real','XMGlobal-Real2','XMGlobal-Real3','XMGlobal-Real4',
    'XMGlobal-Real5','XMGlobal-Real6','XMGlobal-Real7','XMGlobal-Real8',
    'XMGlobal-Demo','XMGlobal-Demo2',
  ]},
  // FP Markets
  { broker: 'FP Markets', servers: [
    'FPMarkets-Live','FPMarkets-Live2','FPMarkets-Live3',
    'FPMarkets-Demo','FPMarkets-Demo2',
  ]},
  // FXTM / ForexTime
  { broker: 'FXTM', servers: [
    'ForexTimeFXTM-Server','ForexTimeFXTM-Server2','ForexTimeFXTM-Demo',
    'FXTM-Real','FXTM-Demo',
  ]},
  // IG Markets
  { broker: 'IG Markets', servers: [
    'IGUKPro-Demo','IGUKPro-Live',
  ]},
  // Exness — correct server names from live broker registry
  { broker: 'Exness', servers: [
    // CY entity
    'ExnessCY-Demo','ExnessCY-LP_Real1',
    // SC entity
    'ExnessSCLtd-Demo','ExnessSCLtd-LP_Real1',
    'ExnessSC-MT5Real','ExnessSC-MT5Real2','ExnessSC-MT5Real3',
    'ExnessSC-MT5Real4','ExnessSC-MT5Real5','ExnessSC-MT5Real6',
    'ExnessSC-MT5Real7','ExnessSC-MT5Real8','ExnessSC-MT5Real9',
    'ExnessSC-MT5Real10','ExnessSC-MT5Real11','ExnessSC-MT5Real12',
    'ExnessSC-MT5Real14','ExnessSC-MT5Real15',
    // UK entity
    'ExnessUK-Demo','ExnessUK-LP_Real1',
    // MU entity
    'ExnessMU-MT5Real','ExnessMU-MT5Real2','ExnessMU-MT5Real3',
    'ExnessMU-MT5Real4','ExnessMU-MT5Real5','ExnessMU-MT5Real6',
    'ExnessMU-MT5Real7','ExnessMU-MT5Real8','ExnessMU-MT5Real9',
    'ExnessMU-MT5Real10','ExnessMU-MT5Real11','ExnessMU-MT5Real12',
    'ExnessMU-MT5Real14','ExnessMU-MT5Real15',
    // KE entity
    'ExnessKE-MT5Real4','ExnessKE-MT5Real9','ExnessKE-MT5Real10','ExnessKE-MT5Real21',
  ]},
  // RoboForex
  { broker: 'RoboForex', servers: [
    'RoboForex-Pro','RoboForex-ProCent','RoboForex-ECN',
    'RoboForex-Demo','RoboForex-DemoPro',
  ]},
  // FxPro
  { broker: 'FxPro', servers: [
    'FxPro.MT5 - Server','FxPro.MT5 - Demo Server',
  ]},
  // HotForex / HF Markets
  { broker: 'HF Markets (HotForex)', servers: [
    'HFMarkets-Live Server','HFMarkets-Demo Server',
    'HFMarketsKE-Live Server','HFMarketsKE-Demo Server',
  ]},
  // Tickmill
  { broker: 'Tickmill', servers: [
    'Tickmill-Live','Tickmill-Demo',
  ]},
  // Vantage FX
  { broker: 'Vantage FX', servers: [
    'Vantage-Live1','Vantage-Live2','Vantage-Live3',
    'Vantage-Demo',
  ]},
  // OANDA
  { broker: 'OANDA', servers: [
    'OANDA-OANDAGroup-1','OANDA-OANDAGroup-2',
  ]},
  // Eightcap
  { broker: 'Eightcap', servers: [
    'Eightcap-Live','Eightcap-Live2','Eightcap-Demo',
  ]},
  // ThinkMarkets
  { broker: 'ThinkMarkets', servers: [
    'ThinkMarkets-Live','ThinkMarkets-Demo',
  ]},
  // Markets.com
  { broker: 'Markets.com', servers: [
    'MarketscomCY-Live','MarketscomCY-Demo',
  ]},
  // Admirals (Admiral Markets)
  { broker: 'Admirals', servers: [
    'Admirals-Demo','Admirals-Live','Admirals-Global-Live','Admirals-Global-Demo',
  ]},
  // EasyMarkets
  { broker: 'EasyMarkets', servers: [
    'EasyMarkets-Live','EasyMarkets-Demo',
  ]},
  // Saxo Bank
  { broker: 'Saxo Bank', servers: [
    'SaxoBankSimulation','SaxoBankLive','SaxoBankSim',
  ]},
  // FXCM
  { broker: 'FXCM', servers: [
    'FXCM-USDReal01','FXCM-USDReal02','FXCM-USDDemo01',
  ]},
  // TMGM
  { broker: 'TMGM', servers: [
    'TMGM-Live01','TMGM-Live02','TMGM-Demo01',
  ]},
  // GO Markets
  { broker: 'GO Markets', servers: [
    'GOMarketsAus-Live01','GOMarketsAus-Demo',
  ]},
  // Axi (AxiCorp)
  { broker: 'Axi', servers: [
    'Axi-Real','Axi-Real2','Axi-Demo',
  ]},
  // ACY Securities
  { broker: 'ACY Securities', servers: [
    'ACY-Live','ACY-Demo',
  ]},
  // Darwinex
  { broker: 'Darwinex', servers: [
    'Darwinex-Demo','Darwinex-Live',
  ]},
  // Fusion Markets
  { broker: 'Fusion Markets', servers: [
    'FusionMarkets-Live','FusionMarkets-Demo',
  ]},
  // Purple Trading
  { broker: 'Purple Trading', servers: [
    'PurpleTrading-Demo','PurpleTrading-Live',
  ]},
  // BlackBull Markets
  { broker: 'BlackBull Markets', servers: [
    'BlackBull-Live','BlackBull-Demo',
  ]},
  // Swissquote
  { broker: 'Swissquote', servers: [
    'Swissquote-BK-Demo','Swissquote-BK-Pro',
  ]},
  // eToro
  { broker: 'eToro', servers: [
    'eToroEU-Server','eToroEU-Demo',
  ]},
  // AvaTrade
  { broker: 'AvaTrade', servers: [
    'AvaTrade-Server','MetaTrader5 Server',
  ]},
  // Capital.com
  { broker: 'Capital.com', servers: [
    'Capital.com-MT5 Live Server','Capital.com-MT5 Demo Server',
  ]},
  // Plus500
  { broker: 'Plus500', servers: [
    'Plus500-Real1','Plus500-Demo',
  ]},
  // Alpari
  { broker: 'Alpari', servers: [
    'AlpariGroupLimited-Real','AlpariGroupLimited-Demo','AlpariLimited-Real',
  ]},
  // Prop firm servers
  { broker: 'FTMO', servers: [
    'FTMO-Demo','FTMO-Server2','FTMO-Server3',
  ]},
  { broker: 'The5%ers', servers: [
    'The5%ers-Server','The5%ers-Demo',
  ]},
  { broker: 'MyForexFunds', servers: [
    'MyForexFunds-Live','MyForexFunds-Demo',
  ]},
  { broker: 'E8 Funding', servers: [
    'E8Funding-Server','E8Funding-Demo',
  ]},
  { broker: 'True Forex Funds', servers: [
    'TrueForexFunds-Live','TrueForexFunds-Demo',
  ]},
  { broker: 'Funded Next', servers: [
    'FundedNext-Server','FundedNext-Demo',
  ]},
  { broker: 'The Funded Trader', servers: [
    'TheFundedTraderProgram-Live','TheFundedTraderProgram-Demo',
  ]},
  { broker: 'Instant Funding', servers: [
    'InstantFunding-Server',
  ]},
  // Bitget (crypto exchange with MT5 gateway)
  { broker: 'Bitget', servers: [
    'Bitget-Live','Bitget-Demo',
    'BitgetFutures-Live','BitgetFutures-Demo',
    'BitgetGlobal-Live','BitgetGlobal-Demo',
  ]},
  // BTGT (Bitget's regulated MT5 broker entities)
  { broker: 'BTGT / Bitget MT5', servers: [
    'BTGTMauritiusCapital-MT5-LIVE1',
    'BTGTMauritiusCapital-MT5-LIVE2',
    'BTGTMauritiusCapital-MT5-LIVE3',
    'BTGTMauritiusCapital-MT5-DEMO1',
    'BTGTMauritiusCapital-MT5-DEMO2',
    'BTGTSCCapital-MT5-LIVE1',
    'BTGTSCCapital-MT5-LIVE2',
    'BTGTSCCapital-MT5-DEMO1',
    'BTGTGlobalCapital-MT5-LIVE1',
    'BTGTGlobalCapital-MT5-DEMO1',
  ]},
]

// Flat sorted list for the combobox
const ALL_SERVERS: { server: string; broker: string }[] = BROKER_SERVERS
  .flatMap(b => b.servers.map(s => ({ server: s, broker: b.broker })))
  .sort((a, b) => a.server.localeCompare(b.server))

/**
 * Look up broker name from a server string using BROKER_SERVERS list.
 * Falls back to parsing the server prefix (e.g. 'ICMarkets-Live01' → 'IC Markets').
 */
function getBrokerFromServer(server: string): string {
  const found = BROKER_SERVERS.find(b => b.servers.some(s => s.toLowerCase() === server.toLowerCase()))
  if (found) return found.broker
  // Partial match (user typed custom server)
  const partial = BROKER_SERVERS.find(b =>
    b.servers.some(s => server.toLowerCase().includes(s.toLowerCase().split('-')[0]))
  )
  if (partial) return partial.broker
  return server
}
function getExchangeFallback(server: string): string | undefined {
  const s = server.toLowerCase()
  // BTGT Capital is a Bitget-affiliated forex broker — use Bitget (credentialed,
  // serves both XAU/USDT candles and ticker) as the default data source.
  if (s.includes('btgt')) return 'bitget'
  if (s.includes('bitget')) return 'bitget'
  if (s.includes('binance')) return 'binance'
  if (s.includes('bybit'))   return 'bybit'
  if (s.includes('okx'))     return 'okx'
  if (s.includes('kucoin'))  return 'kucoin'
  if (s.includes('coinbase')) return 'coinbase'
  // Universal fallback for forex/metals brokers
  return 'bitget'
}

/**
 * Returns true for real MT5 forex/metals brokers (BTGT, ICMarkets, Pepperstone, etc.).
 * These use forex default symbols (XAUUSD, EURUSD) even though some have a crypto
 * exchange parent company.
 * Returns false for pure crypto exchange gateway servers (Bitget-Live, Binance-Live).
 */
function isMT5ForexServer(server: string): boolean {
  const s = server.toLowerCase()
  // Pure crypto exchange servers (no 'capital', 'markets', 'MT5' in name)
  const pureExchangePatterns = [
    'bitget-live', 'bitget-demo', 'bitgetfutures', 'bitgetglobal',
    'binance-live', 'binance-demo',
    'bybit-live', 'bybit-demo',
    'okx-live', 'okx-demo',
    'kucoin-live', 'kucoin-demo',
  ]
  if (pureExchangePatterns.some(p => s.includes(p))) return false
  // Everything else (including BTGT*Capital, ICMarkets, Pepperstone, etc.) is a forex MT5 broker
  return true
}

/**
 * Get the best default chart symbol for a given broker server.
 * BTGT and other forex MT5 brokers → XAUUSD (gold — most popular pair)
 * Pure crypto exchange servers → BTCUSDT
 */
function getDefaultSymbol(server: string): string {
  return isMT5ForexServer(server) ? 'XAUUSD' : 'BTCUSDT'
}

// ── Types ──────────────────────────────────────────────────────────────────────

interface MT5Account {
  id: number
  name: string
  login: string
  server: string
  status: string
  account_type: string
  balance: number
  equity: number
  margin: number
  free_margin: number
  margin_level: number | null
  floating_pnl: number
  currency: string
  leverage: number
  api_reachable: boolean
  last_sync_at: string | null
}

interface MT5Position {
  id: number
  account_id: number
  mt5_ticket: number
  symbol: string
  side: string
  volume: number
  price_open: number
  price_current: number | null
  sl: number | null
  tp: number | null
  swap: number
  profit: number
  commission: number
  comment: string | null
  mt5_time_open: string | null
  rr_ratio: number | null
}

interface MT5Order {
  id: number
  account_id: number
  mt5_ticket: number
  symbol: string
  order_type: string
  volume: number
  price: number
  sl: number | null
  tp: number | null
  status: string
  comment: string | null
}

interface MT5Deal {
  id: number
  account_id: number
  mt5_ticket: number
  symbol: string | null
  deal_type: string
  volume: number | null
  price: number | null
  profit: number
  commission: number
  swap: number
  mt5_time: string | null
}

type AddFormState = {
  name: string
  login: string
  server: string
  password: string
  account_type: string
}

const EMPTY_FORM: AddFormState = { name: '', login: '', server: '', password: '', account_type: 'demo' }

// ── ServerPicker component (inline — only used in this file) ──────────────────

function ServerPicker({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  const [query, setQuery] = useState(value)
  const [open, setOpen]   = useState(false)
  const ref               = useRef<HTMLDivElement>(null)

  // Keep query in sync when parent resets the form
  useEffect(() => { setQuery(value) }, [value])

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const filtered = query.trim() === ''
    ? ALL_SERVERS.slice(0, 80)   // show first 80 when empty
    : ALL_SERVERS.filter(s =>
        s.server.toLowerCase().includes(query.toLowerCase()) ||
        s.broker.toLowerCase().includes(query.toLowerCase())
      ).slice(0, 60)

  const handleSelect = (server: string) => {
    setQuery(server)
    onChange(server)
    setOpen(false)
  }

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setQuery(e.target.value)
    onChange(e.target.value)   // let parent know the raw typed value too
    setOpen(true)
  }

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500 pointer-events-none" />
        <input
          type="text"
          placeholder="Search broker or server…"
          value={query}
          onChange={handleInputChange}
          onFocus={() => setOpen(true)}
          autoComplete="off"
          className="w-full bg-gray-900 border border-gray-700 rounded-lg pl-8 pr-3 py-2 text-white text-sm focus:border-tradebot-accent focus:outline-none"
        />
        {query && (
          <button
            type="button"
            onClick={() => { setQuery(''); onChange(''); setOpen(true) }}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {open && filtered.length > 0 && (
        <div className="absolute z-50 mt-1 w-full bg-gray-800 border border-gray-600 rounded-xl shadow-2xl max-h-64 overflow-y-auto">
          {/* Group by broker */}
          {(() => {
            const groups: Record<string, typeof filtered> = {}
            filtered.forEach(item => {
              if (!groups[item.broker]) groups[item.broker] = []
              groups[item.broker].push(item)
            })
            return Object.entries(groups).map(([broker, items]) => (
              <div key={broker}>
                <div className="px-3 py-1 text-xs font-semibold text-gray-500 bg-gray-800/80 sticky top-0 border-b border-gray-700/40">
                  {broker}
                </div>
                {items.map(item => (
                  <button
                    key={item.server}
                    type="button"
                    onClick={() => handleSelect(item.server)}
                    className={`w-full text-left px-4 py-2 text-sm hover:bg-gray-700 transition-colors ${
                      item.server === value ? 'text-tradebot-accent' : 'text-gray-200'
                    }`}
                  >
                    {item.server}
                    {item.server === value && <span className="ml-2 text-xs text-tradebot-accent">✓</span>}
                  </button>
                ))}
              </div>
            ))
          })()}
          <div className="px-3 py-2 text-xs text-gray-500 border-t border-gray-700/40">
            Not listed? Type any custom server name above.
          </div>
        </div>
      )}
    </div>
  )
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function fmt(n: number) {
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function pnlColor(n: number) {
  return n >= 0 ? 'text-green-400' : 'text-red-400'
}

/** Safely extract a human-readable error string from an axios error.
 *  FastAPI validation errors return detail as an array of {type,loc,msg,...} objects — we join them. */
function apiErr(e: any): string {
  // Connection failures (no response) — most common on Windows when the browser
  // resolves `localhost` to IPv6 while the backend binds IPv4, or when the
  // backend isn't running. Give an actionable message instead of "Network Error".
  if (e?.code === 'ERR_NETWORK' || (e && !e.response && e.request)) {
    return 'Cannot reach the backend API — check that the backend is running on this machine (port 1448).'
  }
  if (e?.code === 'ECONNABORTED') {
    return 'Request timed out — the backend is slow or unreachable.'
  }
  const detail = e?.response?.data?.detail
  if (!detail) return e?.message ?? 'Unknown error'
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((d: any) => d?.msg ?? JSON.stringify(d)).join('; ')
  if (typeof detail === 'object') return detail.msg ?? JSON.stringify(detail)
  return String(detail)
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function MT5LivePage() {
  const [accounts, setAccounts]         = useState<MT5Account[]>([])
  const [selectedId, setSelectedId]     = useState<number | null>(null)
  const [positions, setPositions]       = useState<MT5Position[]>([])
  const [orders, setOrders]             = useState<MT5Order[]>([])
  const [cancelingTicket, setCancelingTicket] = useState<number | null>(null)
  const [closingTicket, setClosingTicket]     = useState<number | null>(null)
  const [closingAll, setClosingAll]           = useState(false)
  const [cancelingAll, setCancelingAll]       = useState(false)
  const [deals, setDeals]               = useState<MT5Deal[]>([])
  // Trade History pagination (client-side over the fetched closed deals).
  const [histPageSize, setHistPageSize] = useState(25)   // rows per page: 10/25/50/100
  const [histPage, setHistPage]         = useState(0)    // 0-indexed page
  const [histSyncing, setHistSyncing]   = useState(false)
  const [histSyncedAt, setHistSyncedAt] = useState<Date | null>(null)
  const [loading, setLoading]           = useState(false)
  const [syncing, setSyncing]           = useState(false)
  const [error, setError]               = useState<string | null>(null)
  const [chartSymbol, setChartSymbol]   = useState('XAUUSD')
  const [chartTimeframe, setChartTimeframe] = useState('H1')
  const [chartMode, setChartMode]       = useState<'advanced' | 'classic' | 'sniper'>('sniper')
  const [showAdd, setShowAdd]           = useState(false)
  const [editingId, setEditingId]       = useState<number | null>(null)
  const [addForm, setAddForm]           = useState<AddFormState>(EMPTY_FORM)
  const [addLoading, setAddLoading]     = useState(false)
  const [addError, setAddError]         = useState<string | null>(null)
  const [testResult, setTestResult]     = useState<{
    reachable: boolean; balance?: number; equity?: number; currency?: string;
    leverage?: number; company?: string; name?: string; error?: string; hint?: string;
    server_suggestions?: { server: string; company: string }[];
  } | null>(null)
  const [testLoading, setTestLoading]   = useState(false)
  // Retest for already-saved accounts
  const [retestId, setRetestId]         = useState<number | null>(null)
  const [retestResult, setRetestResult] = useState<{
    reachable: boolean; balance?: number; currency?: string;
    company?: string; error?: string; hint?: string;
  } | null>(null)
  // Exchange balance data (populated when mtapi-io is unreachable but exchange creds exist)
  const [exchBalance, setExchBalance]   = useState<{
    balance: number; equity: number; available: number; unrealizedPnl: number;
    currency: string; assets: { coin: string; total: number }[]
  } | null>(null)
  const [tradeForm, setTradeForm]       = useState({ symbol: '', volume: '0.01', sl: '', tp: '', operation: 'buy' })
  const [tradeLoading, setTradeLoading] = useState(false)
  const [tradeMsg, setTradeMsg]         = useState<{ ok: boolean; text: string } | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)       // slow full sync
  const fastPollRef = useRef<ReturnType<typeof setInterval> | null>(null)   // fast positions-only
  // ── Auto-Manage Loop state ─────────────────────────────────────────────────
  const [autoManageRunning, setAutoManageRunning] = useState(false)
  const [autoManageLoading, setAutoManageLoading] = useState(false)
  const [autoManageStatus, setAutoManageStatus]   = useState<{
    running: boolean; interval_seconds: number; cooldown_seconds: number;
    started_at: string | null; last_run_at: string | null;
    last_summary: Record<string, unknown> | null; error_count: number;
  } | null>(null)
  const [autoManageMsg, setAutoManageMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const amStatusPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // ── Per-position suggestions from manual analysis ──────────────────────────
  type PositionSuggestion = {
    ticket: number; account_id: number; symbol: string; side: string
    volume: number; price_open: number
    current_sl: number | null; current_tp: number | null
    has_suggestion: boolean
    suggested_sl: number | null; suggested_tp: number | null
    confidence: number | null; rr: number | null; reason: string
  }
  const [positionSuggestions, setPositionSuggestions] = useState<Record<number, PositionSuggestion>>({})
  const [analyzingPositions, setAnalyzingPositions] = useState(false)
  const [applyingAll, setApplyingAll]               = useState(false)
  // Tracks the last account id for which we ran the one-time 24h history load
  // so we don't repeat it on every 8s poll (only on initial account selection).
  const initialSyncDoneRef = useRef<number | null>(null)

  // ── Fetch accounts ────────────────────────────────────────────────────────────

  // Count consecutive backend-unreachable poll failures so we don't flash the
  // error banner on every 8-second tick during a brief outage — only show it
  // after 2+ consecutive failures, and auto-clear it the moment a request succeeds.
  const networkFailCountRef = useRef(0)

  const fetchAccounts = useCallback(async () => {
    try {
      setLoading(true)
      const res = await apiClient.mt5.getAccounts()
      const list: MT5Account[] = res.data
      setAccounts(list)
      networkFailCountRef.current = 0   // backend is reachable — clear any error
      setError(null)
      if (list.length > 0 && selectedId === null) {
        setSelectedId(list[0].id)
      }
    } catch (e: any) {
      networkFailCountRef.current += 1
      // Only surface a hard error after 2 consecutive failures so a single
      // missed request (e.g. backend restart) doesn't alarm the user.
      if (networkFailCountRef.current >= 2) {
        setError(apiErr(e))
      }
    } finally {
      setLoading(false)
    }
  }, [selectedId])

  // ── Fetch positions / orders / deals ──────────────────────────────────────────

  const fetchAccountData = useCallback(async () => {
    if (!selectedId) return
    try {
      // Sync broker → DB first so pending orders / positions are live (the orders
      // and positions endpoints read the cached DB rows). Best-effort: if the
      // sync fails we still read whatever is cached instead of blanking the view.
      try {
        await apiClient.mt5.syncAccount(selectedId)
      } catch { /* non-fatal — fall back to cached data */ }
      // Sync new deals from broker (last 30 days, insert-only for existing tickets).
      // This picks up any new closed trades since the last poll without creating duplicates.
      try {
        await apiClient.mt5.syncDeals(selectedId, false)
      } catch { /* non-fatal — show whatever is in DB */ }
      const [posRes, ordRes, dealRes] = await Promise.all([
        apiClient.mt5.getPositions(selectedId),
        apiClient.mt5.getOrders(selectedId),
        apiClient.mt5.getDeals(selectedId, { limit: 500 }),
      ])
      setPositions(posRes.data)
      setOrders(ordRes.data)
      setDeals(dealRes.data)
      networkFailCountRef.current = 0   // successful data fetch — clear any stale error
      setError(null)
    } catch (e: any) {
      // non-fatal — only show after 2+ consecutive failures to avoid spam
      networkFailCountRef.current += 1
      if (networkFailCountRef.current >= 2) {
        setError(apiErr(e))
      }
    }
  }, [selectedId])

  // Lightweight positions-only refresh (no broker sync / orders / deals) — runs
  // on the fast cadence so floating P&L + margin stay responsive between syncs.
  const fetchPositionsFast = useCallback(async () => {
    if (!selectedId) return
    try {
      const posRes = await apiClient.mt5.getPositions(selectedId)
      setPositions(posRes.data)
    } catch { /* non-fatal — keep last positions */ }
  }, [selectedId])

  useEffect(() => { fetchAccounts() }, [fetchAccounts])
  useEffect(() => { fetchAccountData() }, [fetchAccountData])
  // Reset Trade History to the first page when switching accounts.
  useEffect(() => { setHistPage(0) }, [selectedId])

  // ── One-time 24h history sync on initial account selection ────────────────
  // Shows a visible "Syncing last 24h…" indicator so the user knows trade
  // history is loading.  Runs once per selectedId change, not on every poll.
  useEffect(() => {
    if (!selectedId || initialSyncDoneRef.current === selectedId) return
    initialSyncDoneRef.current = selectedId
    let cancelled = false

    const run = async () => {
      setHistSyncing(true)
      try {
        // Full 30-day sync on initial load — ensures complete month of history
        // is available and stores any new closed trades to Jarvis brain
        await apiClient.mt5.syncDeals(selectedId, true)
        if (cancelled) return
        const dealRes = await apiClient.mt5.getDeals(selectedId, { limit: 500 })
        if (cancelled) return
        setDeals(dealRes.data)
        setHistSyncedAt(new Date())
      } catch {
        /* non-fatal — polling will recover deals on next cycle */
      } finally {
        if (!cancelled) setHistSyncing(false)
      }
    }

    run()
    return () => { cancelled = true }
  }, [selectedId])

  // Split polling: slow full sync (8s) + fast positions-only (2.5s). Both pause
  // while the tab is hidden to save CPU/network, and resume + refresh on return.
  useEffect(() => {
    const startSlow = () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = setInterval(fetchAccountData, 8000)
    }
    const startFast = () => {
      if (fastPollRef.current) clearInterval(fastPollRef.current)
      fastPollRef.current = setInterval(fetchPositionsFast, 5000 * pollMultiplier())
    }
    const stopAll = () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
      if (fastPollRef.current) { clearInterval(fastPollRef.current); fastPollRef.current = null }
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        stopAll()
      } else {
        fetchPositionsFast()
        startSlow(); startFast()
      }
    }
    startSlow(); startFast()
    document.addEventListener('visibilitychange', onVisibility)
    return () => { stopAll(); document.removeEventListener('visibilitychange', onVisibility) }
  }, [fetchAccountData, fetchPositionsFast])

  // ── Per-position manual analysis ──────────────────────────────────────────────
  const handleAnalyzePositions = useCallback(async () => {
    if (!selectedId) return
    setAnalyzingPositions(true)
    setAutoManageMsg(null)
    try {
      const res = await apiClient.mt5.analyzePositions(selectedId)
      const map: Record<number, PositionSuggestion> = {}
      for (const s of (res.data?.suggestions ?? [])) {
        map[s.ticket] = s
      }
      setPositionSuggestions(map)
      const count = res.data?.with_suggestion ?? 0
      setAutoManageMsg({
        ok: true,
        text: count > 0
          ? `Analysis complete — ${count} position${count !== 1 ? 's' : ''} have updated SL/TP suggestions`
          : `Analysis complete — no new suggestions (already optimal or side mismatch)`,
      })
    } catch (e: any) {
      setAutoManageMsg({ ok: false, text: apiErr(e) })
    } finally {
      setAnalyzingPositions(false)
    }
  }, [selectedId])

  const handleApplyAllSuggestions = useCallback(async () => {
    const toApply = Object.values(positionSuggestions).filter(s => s.has_suggestion)
    if (toApply.length === 0) return
    setApplyingAll(true)
    setAutoManageMsg(null)
    try {
      const payload = toApply.map(s => ({
        ticket: s.ticket,
        account_id: s.account_id,
        sl: s.suggested_sl ?? undefined,
        tp: s.suggested_tp ?? undefined,
      }))
      const res = await apiClient.mt5.applySuggestions(payload)
      const applied: number = res.data?.applied ?? 0
      const failed: number = res.data?.failed ?? 0
      setAutoManageMsg({
        ok: failed === 0,
        text: `Applied ${applied} SL/TP update${applied !== 1 ? 's' : ''}${failed > 0 ? ` (${failed} failed)` : ''}`,
      })
      setPositionSuggestions({})
      await fetchAccountData()
    } catch (e: any) {
      setAutoManageMsg({ ok: false, text: apiErr(e) })
    } finally {
      setApplyingAll(false)
    }
  }, [positionSuggestions, fetchAccountData])

  // ── Auto-Manage Loop lifecycle ─────────────────────────────────────────────
  // Poll loop status every 15s to stay in sync; stop loop when page unmounts.

  const fetchAutoManageStatus = useCallback(async () => {
    try {
      const res = await apiClient.mt5.getAutoManageLoopStatus()
      setAutoManageStatus(res.data)
      setAutoManageRunning(res.data?.running ?? false)
    } catch { /* non-fatal */ }
  }, [])

  const toggleAutoManageLoop = useCallback(async () => {
    setAutoManageLoading(true)
    setAutoManageMsg(null)
    try {
      if (autoManageRunning) {
        const res = await apiClient.mt5.stopAutoManageLoop()
        setAutoManageRunning(false)
        setAutoManageStatus(res.data)
        setAutoManageMsg({ ok: true, text: 'Auto-manage loop stopped' })
      } else {
        const res = await apiClient.mt5.startAutoManageLoop(60)
        setAutoManageRunning(true)
        setAutoManageStatus(res.data)
        setAutoManageMsg({ ok: true, text: 'Auto-manage loop started (60s interval)' })
      }
    } catch (e: any) {
      setAutoManageMsg({ ok: false, text: apiErr(e) })
    } finally {
      setAutoManageLoading(false)
    }
  }, [autoManageRunning])

  useEffect(() => {
    // Sync status on mount
    fetchAutoManageStatus()
    // Poll status every 15s
    amStatusPollRef.current = setInterval(fetchAutoManageStatus, 15000 * pollMultiplier())
    return () => {
      // Stop loop when page unmounts
      if (amStatusPollRef.current) clearInterval(amStatusPollRef.current)
      apiClient.mt5.stopAutoManageLoop().catch(() => {/* best-effort */})
    }
  }, [fetchAutoManageStatus])

  // ── Exchange balance fallback when mtapi-io unreachable ─────────────────

  useEffect(() => {
    const account = accounts.find(a => a.id === selectedId)
    if (!account) { setExchBalance(null); return }

    const exch = getExchangeFallback(account.server)
    if (!exch || account.api_reachable) { setExchBalance(null); return }

    let cancelled = false

    const fetchExchBalance = async () => {
      try {
        // Try futures balance first (more relevant for trading accounts)
        if (exch === 'bitget') {
          const [futRes, spotRes] = await Promise.allSettled([
            apiClient.getBitgetFuturesBalance('USDT-FUTURES'),
            apiClient.getBalance(exch),
          ])
          if (cancelled) return

          let balance = 0, equity = 0, available = 0, unrealizedPnl = 0
          const assets: { coin: string; total: number }[] = []

          // Futures
          if (futRes.status === 'fulfilled') {
            const fut = futRes.value.data?.balance?.[0] ?? {}
            equity        = parseFloat(fut.accountEquity   ?? '0')
            available     = parseFloat(fut.available       ?? '0')
            unrealizedPnl = parseFloat(fut.unrealizedPL    ?? '0')
            balance       = equity - unrealizedPnl
          }

          // Spot
          if (spotRes.status === 'fulfilled') {
            const spotBal = spotRes.value.data?.balance ?? {}
            const ignore  = new Set(['info','timestamp','datetime','free','used','total'])
            Object.entries(spotBal).forEach(([coin, data]: [string, any]) => {
              if (ignore.has(coin) || typeof data !== 'object') return
              const total = data.total ?? 0
              if (total > 0.000001) assets.push({ coin, total })
            })
          }

          setExchBalance({ balance, equity, available, unrealizedPnl, currency: 'USDT', assets })
        } else {
          const res = await apiClient.getBalance(exch)
          if (cancelled) return
          const bal = res.data?.balance ?? {}
          const ignore = new Set(['info','timestamp','datetime'])
          const assets: { coin: string; total: number }[] = []
          Object.entries(bal).forEach(([coin, data]: [string, any]) => {
            if (ignore.has(coin) || typeof data !== 'object') return
            const total = data.total ?? 0
            if (total > 0.000001) assets.push({ coin, total })
          })
          const usdtTotal = bal.USDT?.total ?? 0
          setExchBalance({ balance: usdtTotal, equity: usdtTotal, available: bal.USDT?.free ?? 0, unrealizedPnl: 0, currency: 'USDT', assets })
        }
      } catch {
        // Non-fatal — exchange balance fetch failing shouldn't break the page
        if (!cancelled) setExchBalance(null)
      }
    }

    fetchExchBalance()
    const id = setInterval(fetchExchBalance, 15000 * pollMultiplier())   // refresh every 15s
    return () => { cancelled = true; clearInterval(id) }
  }, [selectedId, accounts])

  // ── Actions ───────────────────────────────────────────────────────────────────

  const handleSync = async () => {
    setSyncing(true)
    try {
      await apiClient.mt5.syncAll()
      await fetchAccounts()
      await fetchAccountData()
    } catch (e: any) {
      setError(apiErr(e))
    } finally {
      setSyncing(false)
    }
  }

  const handleRetestAccount = async (accountId: number) => {
    setRetestId(accountId)
    setRetestResult(null)
    try {
      const res = await apiClient.mt5.retestAccount(accountId)
      setRetestResult(res.data)
      // Refresh account list to pick up updated reachable status
      await fetchAccounts()
    } catch (e: any) {
      setRetestResult({ reachable: false, error: apiErr(e) })
    } finally {
      setRetestId(null)
    }
  }

  const handleTestConnection = async () => {
    if (!addForm.login || !addForm.server || !addForm.password) {
      setAddError('Login, server and password are required to test.')
      return
    }
    setTestLoading(true)
    setTestResult(null)
    setAddError(null)
    try {
      const res = await apiClient.mt5.testConnection({
        name: addForm.name || 'test',
        login: addForm.login,
        server: addForm.server,
        password: addForm.password,
        account_type: addForm.account_type,
      })
      setTestResult(res.data)
    } catch (e: any) {
      setTestResult({ reachable: false, error: apiErr(e) })
    } finally {
      setTestLoading(false)
    }
  }

  const openEditAccount = (acc: MT5Account) => {
    setEditingId(acc.id)
    setAddForm({ name: acc.name, login: acc.login, server: acc.server, password: '', account_type: acc.account_type })
    setTestResult(null)
    setAddError(null)
    setShowAdd(true)
  }

  const handleAddAccount = async () => {
    if (!addForm.login || !addForm.server || !addForm.name) {
      setAddError('Name, login and server are required.')
      return
    }
    if (!editingId && !addForm.password) {
      setAddError('Password is required.')
      return
    }
    setAddLoading(true)
    setAddError(null)
    try {
      if (editingId) {
        await apiClient.mt5.updateAccount(editingId, {
          name:         addForm.name,
          login:        addForm.login,
          server:       addForm.server,
          account_type: addForm.account_type,
          ...(addForm.password ? { password: addForm.password } : {}),
        })
      } else {
        await apiClient.mt5.createAccount({
          name:         addForm.name,
          login:        addForm.login,
          server:       addForm.server,
          password:     addForm.password,
          account_type: addForm.account_type,
        })
      }
      setShowAdd(false)
      setEditingId(null)
      setAddForm(EMPTY_FORM)
      setTestResult(null)
      await fetchAccounts()
    } catch (e: any) {
      setAddError(apiErr(e))
    } finally {
      setAddLoading(false)
    }
  }

  const handleDeleteAccount = async (id: number) => {
    if (!confirm('Remove this MT5 account connection? The account itself is not deleted.')) return
    try {
      await apiClient.mt5.deleteAccount(id)
      if (selectedId === id) setSelectedId(null)
      await fetchAccounts()
    } catch (e: any) {
      setError(apiErr(e))
    }
  }

  const handlePlaceTrade = async () => {
    if (!selectedId || !tradeForm.symbol || !tradeForm.volume) return
    setTradeLoading(true)
    setTradeMsg(null)
    try {
      await apiClient.mt5.sendOrder({
        account_id: selectedId,
        symbol: tradeForm.symbol.toUpperCase(),
        operation: tradeForm.operation,
        volume: parseFloat(tradeForm.volume),
        sl: tradeForm.sl ? parseFloat(tradeForm.sl) : undefined,
        tp: tradeForm.tp ? parseFloat(tradeForm.tp) : undefined,
      })
      setTradeMsg({ ok: true, text: `${tradeForm.operation.toUpperCase()} ${tradeForm.volume} ${tradeForm.symbol} sent` })
      await fetchAccountData()
    } catch (e: any) {
      setTradeMsg({ ok: false, text: apiErr(e) })
    } finally {
      setTradeLoading(false)
    }
  }

  const handleChartQuickTrade = async (operation: 'buy' | 'sell', symbol: string, volume: number) => {
    if (!selectedId) return
    setTradeMsg(null)
    try {
      await apiClient.mt5.sendOrder({
        account_id: selectedId,
        symbol: symbol.toUpperCase(),
        operation,
        volume,
      })
      setTradeMsg({ ok: true, text: `${operation.toUpperCase()} ${volume} ${symbol} sent` })
      await fetchAccountData()
    } catch (e: any) {
      setTradeMsg({ ok: false, text: apiErr(e) })
    }
  }

  const handleClosePosition = async (ticket: number) => {
    if (!selectedId) return
    if (!confirm(`Close position #${ticket}?`)) return
    setClosingTicket(ticket)
    try {
      await apiClient.mt5.closeTrade({ account_id: selectedId, ticket })
      await fetchAccountData()
    } catch (e: any) {
      setError(apiErr(e))
    } finally {
      setClosingTicket(null)
    }
  }

  const handleCloseAllPositions = async () => {
    if (!selectedId || positions.length === 0) return
    if (!confirm(`Close ALL ${positions.length} open position${positions.length !== 1 ? 's' : ''}?`)) return
    setClosingAll(true)
    setError(null)
    try {
      const res = await apiClient.mt5.closeAllPositions(selectedId)
      const closed = res.data?.closed_count ?? 0
      const failed = res.data?.failed_count ?? 0
      if (failed > 0) setError(`Closed ${closed} position${closed !== 1 ? 's' : ''}, ${failed} failed`)
      await fetchAccountData()
    } catch (e: any) {
      setError(apiErr(e))
    } finally {
      setClosingAll(false)
    }
  }

  const handleCancelOrder = async (ticket: number) => {
    if (!selectedId) return
    if (!confirm(`Cancel pending order #${ticket}?`)) return
    setCancelingTicket(ticket)
    try {
      await apiClient.mt5.cancelPendingOrder(selectedId, ticket)
      await fetchAccountData()
    } catch (e: any) {
      setError(apiErr(e))
    } finally {
      setCancelingTicket(null)
    }
  }

  const handleCancelAllOrders = async () => {
    if (!selectedId || orders.length === 0) return
    if (!confirm(`Cancel ALL ${orders.length} pending order${orders.length !== 1 ? 's' : ''}?`)) return
    setCancelingAll(true)
    setError(null)
    try {
      const res = await apiClient.mt5.cancelAllPending(selectedId)
      const cancelled = res.data?.cancelled_count ?? 0
      const failed = res.data?.failed_count ?? 0
      if (failed > 0) setError(`Cancelled ${cancelled} order${cancelled !== 1 ? 's' : ''}, ${failed} failed`)
      await fetchAccountData()
    } catch (e: any) {
      setError(apiErr(e))
    } finally {
      setCancelingAll(false)
    }
  }

  // ── Derived ───────────────────────────────────────────────────────────────────

  const selected = accounts.find(a => a.id === selectedId)

  const chartPositions: MT5PositionForChart[] = positions.map(p => ({
    id: p.id, symbol: p.symbol, side: p.side, volume: p.volume,
    price_open: p.price_open, price_current: p.price_current,
    sl: p.sl, tp: p.tp, profit: p.profit, mt5_ticket: p.mt5_ticket,
  }))

  const chartDeals: MT5DealForChart[] = deals
    .filter(d => d.symbol != null)
    .map(d => ({
      id: d.id, symbol: d.symbol!, deal_type: d.deal_type,
      price: d.price, mt5_time: d.mt5_time,
    }))

  // Floating P&L = sum of the broker's real per-position profit (source of truth —
  // includes spread/swap/commission and the broker's exact contract math). We do
  // NOT extrapolate from live price because the broker's price_current is often
  // stuck at the entry price, which makes any price-delta math diverge from the
  // broker. The broker recomputes profit on each sync, so this stays current.
  const brokerFloating = positions.reduce((s, p) => s + p.profit, 0)
  const hasPositions = positions.length > 0

  // Resolved display values.
  // Rule: use exchBalance ONLY when it carries meaningful data (non-zero balance)
  // OR when the MT5 DB has no balance stored yet.
  // This prevents Bitget API returning 0 (no API keys / empty account) from
  // overriding real MT5 broker data that IS stored in the DB.
  const mt5HasBalance = (selected?.balance ?? 0) > 0 || (selected?.equity ?? 0) > 0
  const useExchBal = exchBalance != null && (!mt5HasBalance || (exchBalance.balance ?? 0) > 0)

  const totalFloating = useExchBal ? exchBalance!.unrealizedPnl : brokerFloating

  const displayBalance     = useExchBal ? exchBalance!.balance     : selected?.balance     ?? 0
  // Equity = balance + floating P&L (matches the broker's equity at each sync).
  const displayEquity      = useExchBal
    ? exchBalance!.equity
    : (selected?.balance ?? 0) + totalFloating
  const displayFreeMargin  = useExchBal ? exchBalance!.available   : selected?.free_margin ?? 0
  const displayCurrency    = useExchBal ? exchBalance!.currency    : selected?.currency    ?? 'USD'
  // Margin level = equity / used-margin × 100, recomputed from equity.
  const usedMargin         = selected?.margin ?? 0
  const displayMarginLevel = !useExchBal && usedMargin > 0
    ? (displayEquity / usedMargin) * 100
    : selected?.margin_level ?? null
  const brokerName         = selected ? getBrokerFromServer(selected.server) : ''

  // ── JARVIS context bridge ───────────────────────────────────────────────────────
  // Publish the currently selected MT5 account + chart symbol to the global JARVIS
  // assistant (PaulChat) so it can analyse / place sniper setups for the right
  // account without the user having to specify the symbol. Uses the existing
  // `__jarvisPage` postMessage convention.
  useEffect(() => {
    if (typeof window === 'undefined') return
    if (selectedId == null) return
    window.postMessage({
      __jarvisPage: true,
      type: 'mt5-context',
      accountId: selectedId,
      symbol: chartSymbol,
      timeframe: chartTimeframe,
      balance: displayBalance,
      currency: displayCurrency,
    }, '*')
  }, [selectedId, chartSymbol, chartTimeframe, displayBalance, displayCurrency])

  // Allow JARVIS to request a positions/orders refresh after it places an order
  // from the chat (so the page reflects the new pending order immediately).
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onMsg = (e: MessageEvent) => {
      const d = e.data
      if (d && d.__jarvisPage && d.type === 'mt5-refresh') {
        fetchAccountData()
      }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [fetchAccountData])

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <>
      <Head><title>MT5 Live | TradeBot</title></Head>

      <div className="space-y-4">

        {/* Header */}
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-3">
            <Monitor className="w-6 h-6 text-tradebot-accent" />
            <h1 className="text-2xl font-bold text-white">MT5 Live</h1>
            {accounts.length > 0 && (
              <span className="text-xs text-gray-500">{accounts.length} account{accounts.length !== 1 ? 's' : ''}</span>
            )}
          </div>
          <div className="flex gap-2">
            {/* ── Auto-Manage Loop Toggle ─────────────────────────────── */}
            <button
              onClick={toggleAutoManageLoop}
              disabled={autoManageLoading}
              title={autoManageRunning
                ? `Auto-manage running (60s interval) — click to stop`
                : 'Start auto-manage: periodically updates position TP/SL and pending orders from SMC+AI signals'}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-colors text-sm disabled:opacity-50 ${
                autoManageRunning
                  ? 'bg-emerald-600/30 text-emerald-300 hover:bg-emerald-600/40 border border-emerald-600/40'
                  : 'bg-gray-700/50 text-gray-400 hover:bg-gray-700 border border-gray-600/30'
              }`}
            >
              {autoManageLoading
                ? <span className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                : <span className={`w-2 h-2 rounded-full ${autoManageRunning ? 'bg-emerald-400 animate-pulse' : 'bg-gray-500'}`} />
              }
              {autoManageRunning ? 'Auto-Manage ON' : 'Auto-Manage'}
              {autoManageStatus?.last_run_at && (
                <span className="text-xs opacity-60 hidden sm:inline">
                  · {new Date(autoManageStatus.last_run_at).toLocaleTimeString()}
                </span>
              )}
            </button>
            <button
              onClick={handleSync}
              disabled={syncing}
              className="flex items-center gap-2 px-4 py-2 bg-tradebot-accent/20 text-tradebot-accent rounded-lg hover:bg-tradebot-accent/30 transition-colors disabled:opacity-50 text-sm"
            >
              <RefreshCw className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />
              Sync All
            </button>
            <button
              onClick={() => { setEditingId(null); setAddForm(EMPTY_FORM); setTestResult(null); setShowAdd(true); setAddError(null) }}
              className="flex items-center gap-2 px-4 py-2 bg-green-600/20 text-green-400 rounded-lg hover:bg-green-600/30 transition-colors text-sm"
            >
              <Plus className="w-4 h-4" />
              Add Account
            </button>
          </div>
        </div>

        {/* Error banner — only shown after repeated failures; auto-clears on recovery */}
        {error && (
          <div className="flex items-center gap-2 p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-red-300 text-sm">
            <AlertTriangle className="w-4 h-4 flex-shrink-0" />
            <span className="flex-1">{error}</span>
            <button
              onClick={() => { setError(null); networkFailCountRef.current = 0; fetchAccounts() }}
              className="px-2 py-0.5 text-xs bg-red-800/50 hover:bg-red-700/60 rounded mr-1"
            >
              Retry
            </button>
            <button onClick={() => setError(null)} className="text-red-400 hover:text-red-200">
              <X className="w-4 h-4" />
            </button>
          </div>
        )}

        {/* Auto-manage status / message banner */}
        {autoManageMsg && (
          <div className={`flex items-center gap-2 p-3 rounded-lg text-sm border ${
            autoManageMsg.ok
              ? 'bg-emerald-900/20 border-emerald-700/40 text-emerald-300'
              : 'bg-red-900/20 border-red-700/40 text-red-300'
          }`}>
            <span className="flex-1">{autoManageMsg.text}</span>
            <button onClick={() => setAutoManageMsg(null)} className="opacity-60 hover:opacity-100">
              <X className="w-4 h-4" />
            </button>
          </div>
        )}
        {autoManageRunning && autoManageStatus && (
          <div className="flex items-center gap-3 p-2.5 bg-emerald-900/10 border border-emerald-800/30 rounded-lg text-xs text-emerald-400">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse flex-shrink-0" />
            <span>Auto-manage active — SMC+AI analysis every {autoManageStatus.interval_seconds}s across all accounts</span>
            {autoManageStatus.last_summary && (
              <span className="text-emerald-500/70 hidden md:inline">
                · last: {autoManageStatus.last_summary.position_updates as number ?? 0} pos updated,{' '}
                {(autoManageStatus.last_summary.orders_cancelled as number ?? 0) + (autoManageStatus.last_summary.orders_modified as number ?? 0)} orders actioned
              </span>
            )}
          </div>
        )}

        {/* Add Account form */}
        {showAdd && (
          <div className="p-5 bg-gray-800 border border-gray-700 rounded-xl space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-white font-semibold">{editingId ? 'Edit MT5 Account' : 'Add MT5 Account'}</h3>
              <button onClick={() => { setShowAdd(false); setEditingId(null); setAddForm(EMPTY_FORM); setTestResult(null) }} className="text-gray-400 hover:text-white">
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {/* Account Label */}
              <div>
                <label className="text-xs text-gray-400 mb-1 block">Account Label *</label>
                <input
                  type="text"
                  placeholder="e.g. IC Markets Live"
                  value={addForm.name}
                  onChange={e => setAddForm(p => ({ ...p, name: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-white text-sm focus:border-tradebot-accent focus:outline-none"
                />
              </div>

              {/* MT5 Login */}
              <div>
                <label className="text-xs text-gray-400 mb-1 block">MT5 Login (account number) *</label>
                <input
                  type="text"
                  placeholder="12345678"
                  value={addForm.login}
                  onChange={e => setAddForm(p => ({ ...p, login: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-white text-sm focus:border-tradebot-accent focus:outline-none"
                />
              </div>

              {/* Broker Server — searchable combobox, spans full width */}
              <div className="sm:col-span-2">
                <label className="text-xs text-gray-400 mb-1 block">
                  Broker Server * &nbsp;
                  <span className="text-gray-600">— search by broker name or server string</span>
                </label>
                <ServerPicker
                  value={addForm.server}
                  onChange={v => setAddForm(p => ({ ...p, server: v }))}
                />
              </div>

              {/* Password */}
              <div className="sm:col-span-2">
                <label className="text-xs text-gray-400 mb-1 block">{editingId ? 'Password (leave blank to keep current)' : 'Password *'}</label>
                <input
                  type="password"
                  placeholder={editingId ? '•••••• (unchanged)' : 'MT5 account password'}
                  value={addForm.password}
                  onChange={e => setAddForm(p => ({ ...p, password: e.target.value }))}
                  className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-white text-sm focus:border-tradebot-accent focus:outline-none"
                />
              </div>
            </div>

            <div>
              <label className="text-xs text-gray-400 mb-2 block">Account Type</label>
              <div className="flex gap-4">
                {(['demo', 'live', 'prop'] as const).map(t => (
                  <label key={t} className="flex items-center gap-2 cursor-pointer select-none">
                    <input
                      type="radio"
                      name="account_type"
                      value={t}
                      checked={addForm.account_type === t}
                      onChange={() => setAddForm(p => ({ ...p, account_type: t }))}
                    />
                    <MT5AccountBadge type={t} size="md" />
                  </label>
                ))}
              </div>
              <p className="text-xs text-gray-500 mt-2">
                Requires <strong>mtapi-io</strong> running and connected to your MT5 terminal (default: localhost:8090).
              </p>
            </div>

            {/* Test Connection result panel */}
            {testResult && (
              <div className={`rounded-xl p-4 border text-sm ${
                testResult.reachable
                  ? 'bg-green-900/20 border-green-700/40'
                  : 'bg-red-900/20 border-red-700/40'
              }`}>
                {testResult.reachable ? (
                  <>
                    <div className="flex items-center gap-2 text-green-400 font-semibold mb-2">
                      <CheckCircle className="w-4 h-4" />
                      Connection successful — MT5 account verified
                    </div>
                    <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs">
                      {testResult.company && <><span className="text-gray-400">Company</span><span className="text-white">{testResult.company}</span></>}
                      {testResult.name    && <><span className="text-gray-400">Name</span><span className="text-white font-mono text-xs">{testResult.name}</span></>}
                      {testResult.currency && <><span className="text-gray-400">Balance</span><span className="text-white">{testResult.currency} {(testResult.balance ?? 0).toFixed(2)}</span></>}
                      {testResult.leverage && <><span className="text-gray-400">Leverage</span><span className="text-white">1:{testResult.leverage}</span></>}
                    </div>
                  </>
                ) : (
                  <>
                    <div className="flex items-center gap-2 text-red-400 font-semibold mb-1">
                      <AlertTriangle className="w-4 h-4" />
                      Connection failed
                    </div>
                    <p className="text-red-300 text-xs mb-1">{testResult.error}</p>
                    {testResult.hint && <p className="text-yellow-300 text-xs mb-2">{testResult.hint}</p>}

                    {/* ── Server name suggestions ── */}
                    {(testResult.server_suggestions?.length ?? 0) > 0 && (
                      <div className="mt-2">
                        <p className="text-xs text-gray-400 mb-1.5">
                          Suggested server names for this broker — click to use:
                        </p>
                        <div className="flex flex-wrap gap-1.5 max-h-36 overflow-y-auto">
                          {testResult.server_suggestions!.map((s, i) => (
                            <button
                              key={i}
                              type="button"
                              onClick={() => {
                                setAddForm(p => ({ ...p, server: s.server }))
                                setTestResult(null)
                              }}
                              className="px-2 py-1 text-xs rounded-lg bg-blue-900/40 border border-blue-700/40 text-blue-300 hover:bg-blue-700/40 hover:text-white transition-colors"
                              title={s.company}
                            >
                              {s.server}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Only show setup steps when no server suggestions were found */}
                    {(testResult.server_suggestions?.length ?? 0) === 0 && (
                      <div className="mt-2 p-2 rounded bg-gray-800/50 text-xs text-gray-400 space-y-0.5">
                        <div className="font-medium text-gray-300">How to set up mtapi-io:</div>
                        <div>1. Download from <span className="text-tradebot-accent">mtapi.io</span></div>
                        <div>2. Run: <code className="text-yellow-400">mtapi.exe</code> on your Windows PC with MT5 open</div>
                        <div>3. Set <code className="text-yellow-400">MT5_API_URL=http://YOUR_PC_IP:8092</code> in your .env</div>
                      </div>
                    )}
                  </>
                )}
              </div>
            )}

            {addError && <div className="text-red-400 text-sm">{addError}</div>}

            <div className="flex gap-2 flex-wrap">
              <button
                onClick={handleTestConnection}
                disabled={testLoading || !addForm.login || !addForm.server || !addForm.password}
                className="flex items-center gap-2 px-4 py-2 bg-blue-600/20 text-blue-400 rounded-lg text-sm hover:bg-blue-600/30 disabled:opacity-40 transition-colors border border-blue-700/30"
              >
                {testLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                {testLoading ? 'Testing…' : 'Test Connection'}
              </button>
              <button
                onClick={handleAddAccount}
                disabled={addLoading}
                className="flex items-center gap-2 px-5 py-2 bg-green-600 text-white rounded-lg text-sm hover:bg-green-700 disabled:opacity-50 transition-colors"
              >
                {addLoading && <Loader2 className="w-4 h-4 animate-spin" />}
                {editingId
                  ? (addLoading ? 'Saving…' : 'Save Changes')
                  : (addLoading ? 'Connecting…' : 'Save & Connect')}
              </button>
              <button
                onClick={() => { setShowAdd(false); setEditingId(null); setAddForm(EMPTY_FORM); setTestResult(null) }}
                className="px-4 py-2 bg-gray-700 text-gray-300 rounded-lg text-sm hover:bg-gray-600 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Loading state */}
        {loading && accounts.length === 0 && (
          <div className="flex justify-center py-20">
            <Loader2 className="w-8 h-8 animate-spin text-tradebot-accent" />
          </div>
        )}

        {/* Empty state */}
        {!loading && accounts.length === 0 && (
          <div className="flex flex-col items-center gap-3 py-20 text-center">
            <Monitor className="w-12 h-12 text-gray-700" />
            <p className="text-gray-300 font-medium text-lg">No MT5 accounts connected</p>
            <p className="text-gray-500 text-sm max-w-sm">
              Click <strong className="text-gray-300">Add Account</strong> above. You need{' '}
              <strong className="text-gray-300">mtapi-io</strong> running and your MT5 terminal open.
            </p>
          </div>
        )}

        {/* Account tabs */}
        {accounts.length > 0 && (
          <div className="flex gap-2 overflow-x-auto pb-1">
            {accounts.map(acc => (
              <button
                key={acc.id}
                onClick={() => setSelectedId(acc.id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-xl text-sm whitespace-nowrap transition-all ${
                  selectedId === acc.id
                    ? 'bg-tradebot-accent/20 text-white border border-tradebot-accent/40'
                    : 'bg-gray-800/70 text-gray-400 border border-gray-700 hover:border-gray-600 hover:text-gray-200'
                }`}
              >
                {acc.api_reachable
                  ? <CheckCircle className="w-3 h-3 text-green-400" />
                  : <AlertTriangle className="w-3 h-3 text-yellow-400" />
                }
                <span className="font-medium">{acc.name}</span>
                <span className="text-gray-500 text-xs">#{acc.login}</span>
                <MT5AccountBadge type={acc.account_type} />
                <span
                  role="button"
                  tabIndex={0}
                  aria-label="Edit account"
                  onClick={e => { e.stopPropagation(); openEditAccount(acc) }}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); openEditAccount(acc) } }}
                  className="ml-0.5 text-gray-600 hover:text-tradebot-accent transition-colors cursor-pointer"
                >
                  <Pencil className="w-3 h-3" />
                </span>
                <span
                  role="button"
                  tabIndex={0}
                  aria-label="Remove account"
                  onClick={e => { e.stopPropagation(); handleDeleteAccount(acc.id) }}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); handleDeleteAccount(acc.id) } }}
                  className="ml-0.5 text-gray-600 hover:text-red-400 transition-colors cursor-pointer"
                >
                  <Trash2 className="w-3 h-3" />
                </span>
              </button>
            ))}
          </div>
        )}

        {/* Account dashboard */}
        {selected && (
          <>
            {/* Broker info bar */}
            {(() => {
              const exch = getExchangeFallback(selected.server)
              return (
                <div className="flex items-center gap-3 px-4 py-2.5 bg-gray-800/60 border border-gray-700/50 rounded-xl text-xs flex-wrap">
                  <div className="flex items-center gap-2">
                    <span className="text-gray-400">Broker</span>
                    <span className="text-white font-semibold">{brokerName}</span>
                  </div>
                  <div className="w-px h-3 bg-gray-700" />
                  <div className="flex items-center gap-2">
                    <span className="text-gray-400">Server</span>
                    <span className="font-mono text-gray-200">{selected.server}</span>
                  </div>
                  <div className="w-px h-3 bg-gray-700" />
                  <div className="flex items-center gap-2">
                    <span className="text-gray-400">Login</span>
                    <span className="font-mono text-gray-200">#{selected.login}</span>
                  </div>
                  <div className="w-px h-3 bg-gray-700" />
                  <div className="flex items-center gap-2">
                    <span className="text-gray-400">Data</span>
                    {exch && !selected.api_reachable
                      ? <span className="px-2 py-0.5 rounded-full bg-yellow-900/40 text-yellow-400 border border-yellow-700/40 font-semibold">{exch.toUpperCase()} API</span>
                      : selected.api_reachable
                        ? <span className="px-2 py-0.5 rounded-full bg-green-900/40 text-green-400 border border-green-700/40 font-semibold">MT5 Live</span>
                        : <span className="px-2 py-0.5 rounded-full bg-red-900/40 text-red-400 border border-red-700/40 font-semibold">Offline</span>
                    }
                  </div>
                  <div className="ml-auto flex items-center gap-2">
                    <MT5AccountBadge type={selected.account_type} />
                    {selected.last_sync_at && (
                      <span className="text-gray-500">synced {formatTimeZA(selected.last_sync_at)}</span>
                    )}
                    {/* Retest connection button */}
                    <button
                      onClick={() => handleRetestAccount(selected.id)}
                      disabled={retestId === selected.id}
                      className="flex items-center gap-1.5 px-3 py-1 rounded-lg bg-blue-600/20 text-blue-400 border border-blue-700/30 text-xs hover:bg-blue-600/30 disabled:opacity-50 transition-colors"
                      title="Retest mtapi-io connection with saved credentials"
                    >
                      {retestId === selected.id
                        ? <><Loader2 className="w-3 h-3 animate-spin" /> Testing…</>
                        : <><RefreshCw className="w-3 h-3" /> Retest</>
                      }
                    </button>
                  </div>
                </div>
              )
            })()}

            {/* Retest result panel — shown after clicking Retest on existing account */}
            {retestResult && retestId === null && (
              <div className={`px-4 py-3 rounded-xl border text-sm flex flex-wrap items-center gap-4 ${
                retestResult.reachable
                  ? 'bg-green-900/20 border-green-700/40'
                  : 'bg-red-900/20 border-red-700/40'
              }`}>
                {retestResult.reachable ? (
                  <>
                    <div className="flex items-center gap-2 text-green-400 font-semibold">
                      <CheckCircle className="w-4 h-4" />
                      MT5 connection verified
                    </div>
                    {retestResult.company && (
                      <span className="text-gray-300">{retestResult.company}</span>
                    )}
                    {retestResult.balance !== undefined && (
                      <span className="text-gray-400">
                        Balance: <span className="text-white font-medium">{retestResult.currency} {fmt(retestResult.balance ?? 0)}</span>
                      </span>
                    )}
                    <button onClick={() => setRetestResult(null)} className="ml-auto text-gray-500 hover:text-gray-300">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </>
                ) : (
                  <>
                    <div className="flex items-center gap-2 text-red-400 font-semibold">
                      <AlertTriangle className="w-4 h-4" />
                      Connection failed
                    </div>
                    <span className="text-red-300 text-xs flex-1">{retestResult.error}</span>
                    {retestResult.hint && (
                      <span className="text-gray-500 text-xs w-full">{retestResult.hint}</span>
                    )}
                    <button onClick={() => setRetestResult(null)} className="ml-auto text-gray-500 hover:text-gray-300">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </>
                )}
              </div>
            )}

            {/* Metric cards */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                { label: 'Balance',      value: `${displayCurrency} ${fmt(displayBalance)}`,                                    Icon: DollarSign,   color: 'text-white',                                                                  live: false },
                { label: 'Equity',       value: `${displayCurrency} ${fmt(displayEquity)}`,                                     Icon: TrendingUp,   color: displayEquity >= displayBalance ? 'text-green-400' : 'text-red-400',            live: hasPositions },
                { label: 'Free Margin',  value: `${displayCurrency} ${fmt(displayFreeMargin)}`,                                 Icon: Shield,       color: 'text-blue-400',                                                               live: false },
                { label: 'Floating P&L', value: `${totalFloating >= 0 ? '+' : ''}${displayCurrency} ${fmt(totalFloating)}`,     Icon: totalFloating >= 0 ? TrendingUp : TrendingDown, color: pnlColor(totalFloating),             live: hasPositions },
              ].map(card => (
                <div key={card.label} className="bg-gray-800/50 border border-gray-700/50 rounded-xl p-4">
                  <div className="flex items-center gap-1.5 text-gray-400 text-xs mb-2">
                    <card.Icon className="w-3.5 h-3.5" />
                    {card.label}
                    {card.live && <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse ml-auto" title="Live" />}
                  </div>
                  <div className={`text-lg font-bold tabular-nums ${card.color}`}>{card.value}</div>
                </div>
              ))}
            </div>

            {/* Leverage info + spot assets from exchange */}
            <div className="flex flex-wrap gap-4 text-xs text-gray-500">
              <span>Leverage <span className="text-white font-medium">1:{selected.leverage}</span></span>
              {displayMarginLevel != null && (
                <span className="flex items-center gap-1">
                  Margin Level{' '}
                  <span className={`font-medium tabular-nums ${displayMarginLevel > 200 ? 'text-green-400' : displayMarginLevel > 100 ? 'text-yellow-400' : 'text-red-400'}`}>
                    {displayMarginLevel.toFixed(1)}%
                  </span>
                  {hasPositions && usedMargin > 0 && <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" title="Live" />}
                </span>
              )}
              {exchBalance && exchBalance.assets.length > 0 && (
                <span className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-gray-500">Spot holdings:</span>
                  {exchBalance.assets.slice(0, 6).map(a => (
                    <span key={a.coin} className="px-1.5 py-0.5 rounded bg-gray-800 text-gray-300">
                      {a.coin} {a.total < 0.001 ? a.total.toExponential(2) : a.total.toFixed(4)}
                    </span>
                  ))}
                  {exchBalance.assets.length > 6 && (
                    <span className="text-gray-500">+{exchBalance.assets.length - 6} more</span>
                  )}
                </span>
              )}
            </div>

            {/* Main grid: chart + side panel */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">

              {/* Chart */}
              <div className="lg:col-span-2">
                <div className="flex justify-end mb-2">
                  <div className="flex bg-gray-900/60 rounded-md p-0.5 border border-gray-700/50">
                    <button
                      onClick={() => setChartMode('sniper')}
                      className={`px-2.5 py-0.5 rounded text-[11px] font-medium transition ${chartMode === 'sniper' ? 'bg-tradebot-accent/30 text-tradebot-accent' : 'text-gray-400 hover:text-white'}`}
                    >
                      Sniper
                    </button>
                    <button
                      onClick={() => setChartMode('advanced')}
                      className={`px-2.5 py-0.5 rounded text-[11px] font-medium transition ${chartMode === 'advanced' ? 'bg-tradebot-accent/30 text-tradebot-accent' : 'text-gray-400 hover:text-white'}`}
                    >
                      Advanced
                    </button>
                    <button
                      onClick={() => setChartMode('classic')}
                      className={`px-2.5 py-0.5 rounded text-[11px] font-medium transition ${chartMode === 'classic' ? 'bg-tradebot-accent/30 text-tradebot-accent' : 'text-gray-400 hover:text-white'}`}
                    >
                      Classic
                    </button>
                  </div>
                </div>
                {chartMode === 'sniper' ? (
                  <ChartErrorBoundary label="The sniper chart hit a rendering error.">
                    <MT5SniperChart
                      accountId={selected.id}
                      defaultSymbol={getDefaultSymbol(selected.server)}
                      accountBalance={selected.balance ?? 0}
                      accountCurrency={selected.currency ?? 'USD'}
                      fallbackExchange={getExchangeFallback(selected.server)}
                      orders={orders}
                      onCancelOrder={handleCancelOrder}
                      onPlaced={fetchAccountData}
                      positions={positions}
                      onSymbolChange={s => { setChartSymbol(s); setTradeForm(p => ({ ...p, symbol: s })) }}
                      onTimeframeChange={tf => setChartTimeframe(tf)}
                    />
                  </ChartErrorBoundary>
                ) : chartMode === 'advanced' ? (
                  <MT5AdvancedChart
                    accountId={selected.id}
                    defaultSymbol={getDefaultSymbol(selected.server)}
                    positions={chartPositions}
                    deals={chartDeals}
                    onSymbolChange={s => { setChartSymbol(s); setTradeForm(p => ({ ...p, symbol: s })) }}
                    fallbackExchange={getExchangeFallback(selected.server)}
                    preferForexSymbols={isMT5ForexServer(selected.server)}
                    onQuickTrade={handleChartQuickTrade}
                  />
                ) : (
                  <MT5Chart
                    accountId={selected.id}
                    defaultSymbol={getDefaultSymbol(selected.server)}
                    positions={chartPositions}
                    deals={chartDeals}
                    onSymbolChange={s => { setChartSymbol(s); setTradeForm(p => ({ ...p, symbol: s })) }}
                    fallbackExchange={getExchangeFallback(selected.server)}
                    preferForexSymbols={isMT5ForexServer(selected.server)}
                  />
                )}

                {/* Autonomous Scalp Bot — analyses all timeframes, auto-trades with SL/TP + recovery */}
                <div className="mt-4">
                  <MT5ScalpBotPanel
                    key={selected.id}
                    accountId={selected.id}
                    accountType={selected.account_type}
                    serverSymbolDefault={getDefaultSymbol(selected.server)}
                    chartSymbol={chartSymbol}
                    onSymbolChange={s => {
                      setChartSymbol(s)
                      setTradeForm(p => ({ ...p, symbol: s }))
                    }}
                  />
                </div>
              </div>

              {/* Side panel */}
              <div className="flex flex-col gap-4">

                {/* Open Positions */}
                <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-700/40 flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm font-medium text-white">
                      <ArrowUpDown className="w-4 h-4 text-tradebot-accent" />
                      Positions
                      <span className="text-xs text-gray-400">({positions.length})</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`text-xs font-bold ${pnlColor(totalFloating)}`}>
                        {totalFloating >= 0 ? '+' : ''}{fmt(totalFloating)}
                      </span>
                      {/* Close All positions */}
                      {positions.length > 0 && selectedId && (
                        <button
                          onClick={handleCloseAllPositions}
                          disabled={closingAll}
                          title="Close all open positions"
                          className="flex items-center gap-1 px-2.5 py-1 rounded bg-red-600/25 text-red-300 hover:bg-red-600/35 disabled:opacity-50 transition-colors text-xs border border-red-600/30"
                        >
                          {closingAll
                            ? <span className="w-3 h-3 border border-current border-t-transparent rounded-full animate-spin" />
                            : <X className="w-3 h-3" />
                          }
                          Close All
                        </button>
                      )}
                      {/* Analyse button — only when there are positions */}
                      {positions.length > 0 && selectedId && (
                        <button
                          onClick={handleAnalyzePositions}
                          disabled={analyzingPositions || applyingAll}
                          title="Run SMC+AI analysis on all open positions and get SL/TP suggestions"
                          className="flex items-center gap-1 px-2.5 py-1 rounded bg-tradebot-accent/20 text-tradebot-accent hover:bg-tradebot-accent/30 disabled:opacity-50 transition-colors text-xs"
                        >
                          {analyzingPositions
                            ? <span className="w-3 h-3 border border-current border-t-transparent rounded-full animate-spin" />
                            : <RefreshCw className="w-3 h-3" />
                          }
                          Analyse All
                        </button>
                      )}
                      {/* Apply All button — only when there are actionable suggestions */}
                      {Object.values(positionSuggestions).some(s => s.has_suggestion) && (
                        <button
                          onClick={handleApplyAllSuggestions}
                          disabled={applyingAll || analyzingPositions}
                          title="Apply all suggested SL/TP updates to open positions"
                          className="flex items-center gap-1 px-2.5 py-1 rounded bg-emerald-600/25 text-emerald-300 hover:bg-emerald-600/35 disabled:opacity-50 transition-colors text-xs border border-emerald-600/30"
                        >
                          {applyingAll
                            ? <span className="w-3 h-3 border border-current border-t-transparent rounded-full animate-spin" />
                            : <span className="text-xs">✓</span>
                          }
                          Apply All ({Object.values(positionSuggestions).filter(s => s.has_suggestion).length})
                        </button>
                      )}
                    </div>
                  </div>

                  {positions.length === 0 ? (
                    <div className="p-8 text-center text-gray-500 text-sm">No open positions</div>
                  ) : (
                    <div className="divide-y divide-gray-700/30 max-h-72 overflow-y-auto">
                      {positions.map(pos => {
                        const sug = positionSuggestions[pos.mt5_ticket]
                        return (
                        <div
                          key={pos.id}
                          role="button"
                          tabIndex={0}
                          onClick={() => setChartSymbol(pos.symbol)}
                          className={`w-full text-left px-4 py-3 hover:bg-gray-700/30 transition-colors cursor-pointer ${
                            chartSymbol === pos.symbol ? 'bg-gray-700/20' : ''
                          }`}
                        >
                          <div className="flex items-center justify-between">
                            <div className="flex items-center gap-2">
                              <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                                pos.side === 'buy' ? 'bg-blue-900/30 text-blue-400' : 'bg-orange-900/30 text-orange-400'
                              }`}>
                                {pos.side.toUpperCase()}
                              </span>
                              <span className="text-white text-sm font-medium">{pos.symbol}</span>
                              <span className="text-gray-500 text-xs">{pos.volume}L</span>
                              {/* Suggestion badge */}
                              {sug && sug.has_suggestion && (
                                <span className="text-xs px-1.5 py-0.5 rounded bg-emerald-900/30 text-emerald-400 border border-emerald-700/30">
                                  New SL/TP
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-2">
                              <span className={`text-sm font-bold ${pnlColor(pos.profit)}`}>
                                {pos.profit >= 0 ? '+' : ''}{fmt(pos.profit)}
                              </span>
                              <button
                                onClick={(e) => { e.stopPropagation(); handleClosePosition(pos.mt5_ticket) }}
                                disabled={closingTicket === pos.mt5_ticket || closingAll}
                                title="Close this position"
                                className="flex items-center gap-1 px-2 py-0.5 rounded bg-red-600/80 hover:bg-red-600 text-white text-xs disabled:opacity-50"
                              >
                                <X className="w-3 h-3" />
                                {closingTicket === pos.mt5_ticket ? '...' : 'Close'}
                              </button>
                            </div>
                          </div>
                          <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
                            <span>@ {pos.price_open}</span>
                            {pos.sl && <span className="text-red-400/70">SL {pos.sl}</span>}
                            {pos.tp && <span className="text-green-400/70">TP {pos.tp}</span>}
                            {pos.rr_ratio != null && <span>R:R {pos.rr_ratio}</span>}
                            {chartSymbol === pos.symbol && (
                              <span className="ml-auto text-tradebot-accent">on chart ↑</span>
                            )}
                          </div>
                          {/* Suggestion details row */}
                          {sug && sug.has_suggestion && (
                            <div className="mt-1.5 flex items-center gap-2 text-xs">
                              <span className="text-emerald-400/80">
                                → SL {sug.suggested_sl?.toFixed(5)} · TP {sug.suggested_tp?.toFixed(5)}
                              </span>
                              {sug.confidence != null && (
                                <span className="text-gray-500">{(sug.confidence * 100).toFixed(0)}% conf</span>
                              )}
                              {sug.rr != null && (
                                <span className="text-gray-500">R:R {sug.rr}</span>
                              )}
                            </div>
                          )}
                          {/* Side mismatch / no suggestion note */}
                          {sug && !sug.has_suggestion && sug.reason && (
                            <div className="mt-1 text-xs text-gray-600 truncate" title={sug.reason}>
                              ℹ {sug.reason}
                            </div>
                          )}
                        </div>
                        )
                      })}
                    </div>
                  )}
                </div>

                {/* Pending Orders */}
                <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-700/40 flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm font-medium text-white">
                      <BarChart2 className="w-4 h-4 text-yellow-400" />
                      Pending Orders
                      <span className="text-xs text-gray-400">({orders.length})</span>
                    </div>
                    {orders.length > 0 && selectedId && (
                      <button
                        onClick={handleCancelAllOrders}
                        disabled={cancelingAll}
                        title="Cancel all pending orders"
                        className="flex items-center gap-1 px-2.5 py-1 rounded bg-red-600/25 text-red-300 hover:bg-red-600/35 disabled:opacity-50 transition-colors text-xs border border-red-600/30"
                      >
                        {cancelingAll
                          ? <span className="w-3 h-3 border border-current border-t-transparent rounded-full animate-spin" />
                          : <X className="w-3 h-3" />
                        }
                        Cancel All
                      </button>
                    )}
                  </div>

                  {orders.length === 0 ? (
                    <div className="p-8 text-center text-gray-500 text-sm">No pending orders</div>
                  ) : (
                    <div className="divide-y divide-gray-700/30 max-h-56 overflow-y-auto">
                      {orders.map(ord => (
                        <div key={ord.id} className="px-4 py-3">
                          <div className="flex items-center justify-between">
                            <div className="flex items-center gap-2">
                              <span className="text-xs text-gray-500 font-mono">#{ord.mt5_ticket}</span>
                              <span className="text-white text-sm font-medium">{ord.symbol}</span>
                            </div>
                            <div className="flex items-center gap-2">
                              <span className="text-xs text-gray-400 uppercase">
                                {ord.order_type.replace('_', ' ')}
                              </span>
                              <button
                                onClick={() => handleCancelOrder(ord.mt5_ticket)}
                                disabled={cancelingTicket === ord.mt5_ticket}
                                title="Cancel order"
                                className="flex items-center gap-1 px-2 py-0.5 rounded bg-red-600/80 hover:bg-red-600 text-white text-xs disabled:opacity-50"
                              >
                                <X className="w-3 h-3" />
                                {cancelingTicket === ord.mt5_ticket ? '...' : 'Cancel'}
                              </button>
                            </div>
                          </div>
                          <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
                            <span>{ord.volume}L @ {ord.price}</span>
                            {ord.sl && <span className="text-red-400/70">SL {ord.sl}</span>}
                            {ord.tp && <span className="text-green-400/70">TP {ord.tp}</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Trade Panel */}
                <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-700/40 text-sm font-medium text-white flex items-center gap-2">
                    <TrendingUp className="w-4 h-4 text-tradebot-accent" />
                    Place Order
                    {!selected?.api_reachable && (
                      <span className="ml-auto text-xs text-yellow-400/70">Requires MT5 connection</span>
                    )}
                  </div>
                  <div className="p-3 space-y-2">
                    {/* Operation tabs */}
                    <div className="flex gap-1">
                      {(['buy','sell','buy_limit','sell_limit','buy_stop','sell_stop'] as const).map(op => (
                        <button
                          key={op}
                          onClick={() => setTradeForm(p => ({ ...p, operation: op }))}
                          className={`flex-1 px-1.5 py-1 rounded text-xs font-semibold transition-colors ${
                            tradeForm.operation === op
                              ? op.startsWith('buy') ? 'bg-blue-600 text-white' : 'bg-orange-600 text-white'
                              : 'bg-gray-700/50 text-gray-400 hover:text-gray-200'
                          }`}
                        >
                          {op.replace('_',' ').toUpperCase().replace(' LIMIT','L').replace(' STOP','S')}
                        </button>
                      ))}
                    </div>
                    {/* Symbol + Volume */}
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="text-xs text-gray-500 mb-0.5 block">Symbol</label>
                        <input
                          value={tradeForm.symbol}
                          onChange={e => setTradeForm(p => ({ ...p, symbol: e.target.value.toUpperCase() }))}
                          placeholder={chartSymbol || 'XAUUSD'}
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-white text-xs focus:border-tradebot-accent focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-500 mb-0.5 block">Volume (lots)</label>
                        <input
                          value={tradeForm.volume}
                          onChange={e => setTradeForm(p => ({ ...p, volume: e.target.value }))}
                          type="number" step="0.01" min="0.01"
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-white text-xs focus:border-tradebot-accent focus:outline-none"
                        />
                      </div>
                    </div>
                    {/* SL + TP */}
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="text-xs text-gray-500 mb-0.5 block">Stop Loss</label>
                        <input
                          value={tradeForm.sl}
                          onChange={e => setTradeForm(p => ({ ...p, sl: e.target.value }))}
                          type="number" step="any" placeholder="optional"
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-white text-xs placeholder-gray-600 focus:border-red-500 focus:outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-gray-500 mb-0.5 block">Take Profit</label>
                        <input
                          value={tradeForm.tp}
                          onChange={e => setTradeForm(p => ({ ...p, tp: e.target.value }))}
                          type="number" step="any" placeholder="optional"
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-white text-xs placeholder-gray-600 focus:border-green-500 focus:outline-none"
                        />
                      </div>
                    </div>
                    {/* Submit */}
                    <button
                      onClick={handlePlaceTrade}
                      disabled={tradeLoading || !selectedId}
                      className={`w-full py-2 rounded-lg text-sm font-bold transition-colors disabled:opacity-50 ${
                        tradeForm.operation.startsWith('buy')
                          ? 'bg-blue-600 hover:bg-blue-500 text-white'
                          : 'bg-orange-600 hover:bg-orange-500 text-white'
                      }`}
                    >
                      {tradeLoading
                        ? 'Sending…'
                        : `${tradeForm.operation.replace('_',' ').toUpperCase()} ${tradeForm.volume || '?'} ${tradeForm.symbol || 'symbol'}`
                      }
                    </button>
                    {tradeMsg && (
                      <div className={`text-xs px-2 py-1.5 rounded ${tradeMsg.ok ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
                        {tradeMsg.text}
                      </div>
                    )}
                  </div>
                </div>

              </div>
            </div>

            {/* ── Trade History ────────────────────────────────────────────── */}
            {(() => {
              // Only show trade deals (buy/sell) — filter out balance, commission, swap entries
              const tradeDeal = deals.filter(d => d.symbol && (d.deal_type === 'buy' || d.deal_type === 'sell'))
              const totalPnL  = tradeDeal.reduce((s, d) => s + d.profit + d.commission + d.swap, 0)
              const winCount  = tradeDeal.filter(d => d.profit > 0).length
              const winRate   = tradeDeal.length > 0 ? Math.round((winCount / tradeDeal.length) * 100) : 0
              // Client-side pagination over the fetched closed-trade history.
              const totalPages = Math.max(1, Math.ceil(tradeDeal.length / histPageSize))
              const pageIdx    = Math.min(histPage, totalPages - 1)
              const startRow   = pageIdx * histPageSize
              const pageDeals  = tradeDeal.slice(startRow, startRow + histPageSize)

              return (
                <div className="bg-gray-800/50 border border-gray-700/50 rounded-xl overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-700/40 flex items-center justify-between flex-wrap gap-2">
                    <div className="flex items-center gap-2 text-sm font-medium text-white">
                      <BarChart2 className="w-4 h-4 text-tradebot-accent" />
                      Trade History
                      <span className="text-xs text-gray-400">({tradeDeal.length} deals)</span>
                    </div>
                    <div className="flex items-center gap-3 text-xs flex-wrap">
                      {/* Manual sync button — fetches full 90-day history from broker */}
                      <button
                        onClick={async () => {
                          if (!selectedId || histSyncing) return
                          setHistSyncing(true)
                          try {
                            // Full 90-day sync — gets complete broker history + stores to brain
                            await (apiClient.mt5 as any).syncDealsFullHistory(selectedId)
                            const dealRes = await apiClient.mt5.getDeals(selectedId, { limit: 500 })
                            setDeals(dealRes.data)
                            setHistSyncedAt(new Date())
                          } catch { /* non-fatal */ }
                          finally { setHistSyncing(false) }
                        }}
                        disabled={histSyncing || !selectedId}
                        className="flex items-center gap-1 px-2.5 py-1 rounded-md border border-gray-700 text-gray-400 hover:border-tradebot-accent/60 hover:text-white transition disabled:opacity-40"
                        title="Fetch full 90-day trade history from broker and store to Jarvis brain"
                      >
                        {histSyncing
                          ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          : <RefreshCw className="w-3.5 h-3.5" />}
                        {histSyncing ? 'Syncing…' : 'Sync history'}
                      </button>
                      {/* Analyze historical trades → Jarvis brain */}
                      <button
                        onClick={async () => {
                          if (!selectedId) return
                          try {
                            const res = await (apiClient.mt5 as any).analyzeTradeHistory(selectedId, 50)
                            alert(`✅ Queued ${res.data?.queued || 0} trades for Jarvis brain analysis`)
                          } catch { alert('Failed to queue analysis') }
                        }}
                        disabled={!selectedId || tradeDeal.length === 0}
                        className="flex items-center gap-1 px-2.5 py-1 rounded-md border border-purple-700/50 text-purple-400 hover:border-purple-500 hover:text-purple-300 transition disabled:opacity-40"
                        title="Send last 50 closed trades to Jarvis brain for AI analysis with news context"
                      >
                        <Brain className="w-3.5 h-3.5" />
                        Analyze to Brain
                      </button>
                      {histSyncedAt && (
                        <span className="text-gray-500 text-[11px]">
                          synced {histSyncedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        </span>
                      )}
                      {tradeDeal.length > 0 && (
                        <>
                          <span className="text-gray-400">
                            Win rate <span className={`font-bold ${winRate >= 50 ? 'text-green-400' : 'text-red-400'}`}>{winRate}%</span>
                          </span>
                          <span className="text-gray-400">
                            Net P&amp;L{' '}
                            <span className={`font-bold ${pnlColor(totalPnL)}`}>
                              {totalPnL >= 0 ? '+' : ''}{selected.currency} {fmt(totalPnL)}
                            </span>
                          </span>
                          <label className="text-gray-400 flex items-center gap-1">
                            Rows
                            <select
                              value={histPageSize}
                              onChange={(e) => { setHistPageSize(Number(e.target.value)); setHistPage(0) }}
                              className="bg-gray-900 border border-gray-700 rounded px-1.5 py-0.5 text-gray-200 focus:outline-none focus:border-tradebot-accent"
                            >
                              {[10, 25, 50, 100].map(n => <option key={n} value={n}>{n}</option>)}
                            </select>
                          </label>
                        </>
                      )}
                    </div>
                  </div>

                  {tradeDeal.length === 0 ? (
                    <div className="p-8 text-center text-gray-500 text-sm">No deal history. Sync the account to load history.</div>
                  ) : (
                    <>
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-gray-500 text-xs border-b border-gray-700/40">
                            <th className="text-left px-4 py-2">Ticket</th>
                            <th className="text-left px-4 py-2">Time</th>
                            <th className="text-left px-4 py-2">Symbol</th>
                            <th className="text-left px-4 py-2">Type</th>
                            <th className="text-right px-4 py-2">Volume</th>
                            <th className="text-right px-4 py-2">Price</th>
                            <th className="text-right px-4 py-2">Commission</th>
                            <th className="text-right px-4 py-2">Swap</th>
                            <th className="text-right px-4 py-2">P&amp;L</th>
                            <th className="text-right px-4 py-2">Net</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-700/20">
                          {pageDeals.map(d => {
                            const net = d.profit + d.commission + d.swap
                            return (
                              <tr key={d.id} className="hover:bg-gray-700/20 transition-colors">
                                <td className="px-4 py-2 font-mono text-xs text-gray-500">#{d.mt5_ticket}</td>
                                <td className="px-4 py-2 text-gray-400 text-xs whitespace-nowrap">
                                  {d.mt5_time ? formatDateTimeCompactZA(d.mt5_time) : '—'}
                                </td>
                                <td className="px-4 py-2 font-medium text-white">{d.symbol ?? '—'}</td>
                                <td className="px-4 py-2">
                                  <span className={`px-1.5 py-0.5 rounded text-xs font-semibold ${
                                    d.deal_type === 'buy' ? 'bg-blue-900/30 text-blue-400' : 'bg-orange-900/30 text-orange-400'
                                  }`}>
                                    {d.deal_type.toUpperCase()}
                                  </span>
                                </td>
                                <td className="px-4 py-2 text-right text-gray-300">{d.volume ?? '—'}</td>
                                <td className="px-4 py-2 text-right text-gray-300">{d.price ?? '—'}</td>
                                <td className="px-4 py-2 text-right text-red-400/70 text-xs">
                                  {d.commission !== 0 ? fmt(d.commission) : '—'}
                                </td>
                                <td className="px-4 py-2 text-right text-yellow-400/70 text-xs">
                                  {d.swap !== 0 ? fmt(d.swap) : '—'}
                                </td>
                                <td className={`px-4 py-2 text-right font-medium ${pnlColor(d.profit)}`}>
                                  {d.profit >= 0 ? '+' : ''}{fmt(d.profit)}
                                </td>
                                <td className={`px-4 py-2 text-right font-bold text-xs ${pnlColor(net)}`}>
                                  {net >= 0 ? '+' : ''}{fmt(net)}
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                    <div className="flex items-center justify-between px-4 py-3 border-t border-gray-700/40 text-xs">
                      <span className="text-gray-500">
                        Showing {startRow + 1}&ndash;{Math.min(startRow + histPageSize, tradeDeal.length)} of {tradeDeal.length}
                      </span>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => setHistPage(Math.max(0, pageIdx - 1))}
                          disabled={pageIdx === 0}
                          className="px-2.5 py-1 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                        >
                          <ChevronLeft className="w-3.5 h-3.5" /> Back
                        </button>
                        <span className="text-gray-400">Page {pageIdx + 1} / {totalPages}</span>
                        <button
                          onClick={() => setHistPage(Math.min(totalPages - 1, pageIdx + 1))}
                          disabled={pageIdx >= totalPages - 1}
                          className="px-2.5 py-1 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
                        >
                          Next <ChevronRight className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>
                    </>
                  )}
                </div>
              )
            })()}
          </>
        )}

      </div>
    </>
  )
}
