import Head from 'next/head'
import dynamic from 'next/dynamic'
import { useState, useEffect, useCallback, useRef } from 'react'
import { useTradeStore } from '@/store/useTradeStore'
import { apiClient } from '@/services/api'
import { formatPrice } from '@/utils/price'
import { formatDateTimeZA, formatTimeZA } from '@/utils/datetime'
import {
  SMART_MONEY_CONCEPTS_STUDY_ID,
  formatTradingViewStudyLabel,
  normalizeTradingViewStudies,
  normalizeTradingViewStudyId,
} from '@/utils/tradingviewStudies'
import { useZarRate } from '@/hooks/useZarRate'
import SignalFeed from '@/components/SignalFeed'
import {
  TrendingUp,
  TrendingDown,
  Zap,
  DollarSign,
  AlertTriangle,
  RefreshCw,
  CheckCircle,
  XCircle,
  ArrowRight,
  Play,
  Pause,
  Plus,
  RotateCcw,
  Shield,
  Settings,
  Target,
  Activity,
  Wallet,
  Search,
  X,
} from 'lucide-react'

const TradingViewChart = dynamic(() => import('@/components/TradingViewChart'), {
  ssr: false,
})
const TradingViewWidget = dynamic(() => import('@/components/TradingViewWidget'), {
  ssr: false,
})

const DEFAULT_PAIRS = [
  'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT',
  'XRP/USDT', 'ADA/USDT', 'DOGE/USDT', 'AVAX/USDT',
  'DOT/USDT', 'LINK/USDT', 'MATIC/USDT', 'NEAR/USDT',
  'ARB/USDT', 'OP/USDT', 'PEPE/USDT', 'SHIB/USDT',
  'FLOKI/USDT', 'WIF/USDT', 'BONK/USDT',
]

const timeframes = [
  { label: '1m', value: '1m' },
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
  { label: '4h', value: '4h' },
  { label: '1d', value: '1d' },
]

interface TickerData {
  symbol: string
  last: number
  bid: number
  ask: number
  high: number
  low: number
  volume: number
  change: number
  percentage: number
}

interface Signal {
  id: number
  symbol: string
  action: string
  source: string
  price: number
  confidence: number
  strength: number
  status: string
  created_at: string
  timeframe?: string
  indicators?: string
  raw_data?: string
}

interface OrderResult {
  success: boolean
  message: string
  data?: any
}

interface SimAccount {
  id: number
  name: string
  is_active: boolean
  balance: number
  equity: number
  initial_balance: number
  total_pnl: number
  total_pnl_pct: number
  unrealized_pnl: number
  reserved_margin: number
  open_positions_count: number
  total_trades: number
  winning_trades: number
  losing_trades: number
  win_rate: number
  auto_trade: boolean
  auto_trade_pairs: string[]
  auto_trade_timeframe: string
  auto_trade_max_positions: number
  auto_trade_risk_pct: number
  auto_trade_mode: string
  auto_trade_leverage: number
  auto_trade_margin_mode: string
  auto_trade_amount_mode: string
  auto_trade_pine_script_id: number | null
  enable_ai: boolean
}

interface SimPosition {
  id: number
  symbol: string
  side: string
  amount: number
  entry_price: number
  current_price: number
  stop_loss: number | null
  take_profit: number | null
  sl_type: string | null
  trade_type: string
  margin_mode: string | null
  leverage: number | null
  margin: number
  unrealized_pnl: number
  unrealized_roe_pct: number
  realized_pnl: number | null
  realized_roe_pct: number | null
  closed_at: string | null
  created_at: string
  status: string
}

interface SimOrder {
  id: number
  symbol: string
  side: string
  order_type: string
  amount: number
  price: number
  cost: number
  stop_loss: number | null
  take_profit: number | null
  sl_type: string | null
  trade_type: string
  margin_mode: string | null
  leverage: number | null
  status: string
  created_at: string
}

export default function TradingPage() {
  const {
    selectedSymbol, setSelectedSymbol,
    selectedExchange, setSelectedExchange,
    selectedTimeframe, setSelectedTimeframe,
    tradingMode, setTradingMode,
  } = useTradeStore()

  const [mounted, setMounted] = useState(false)
  const [configuredPairs, setConfiguredPairs] = useState<string[]>(DEFAULT_PAIRS)
  const { toZar } = useZarRate()

  // Chart pair search
  const [chartPairQuery, setChartPairQuery] = useState('')
  const [chartPairOpen, setChartPairOpen] = useState(false)
  const chartPairRef = useRef<HTMLDivElement>(null)

  // Ticker
  const [ticker, setTicker] = useState<TickerData | null>(null)
  const [tickerLoading, setTickerLoading] = useState(false)

  // Order form
  const [orderSide, setOrderSide] = useState<'buy' | 'sell'>('buy')
  const [orderType, setOrderType] = useState<'market' | 'limit'>('market')
  const [orderAmount, setOrderAmount] = useState('')
  const [orderPrice, setOrderPrice] = useState('')
  const [orderLoading, setOrderLoading] = useState(false)
  const [orderResult, setOrderResult] = useState<OrderResult | null>(null)
  const [isDryRun, setIsDryRun] = useState(true)
  const [orderTradeType, setOrderTradeType] = useState<'spot' | 'futures'>('spot')
  const [orderLeverage, setOrderLeverage] = useState(10)
  const [orderMarginMode, setOrderMarginMode] = useState<'crossed' | 'isolated'>('crossed')
  const [orderAmountMode, setOrderAmountMode] = useState<'base' | 'quote'>('base')
  const [orderSlPct, setOrderSlPct] = useState('2')
  const [orderTpPct, setOrderTpPct] = useState('4')
  const [orderAutoSlTp, setOrderAutoSlTp] = useState(true)

  // Signals for execution
  const [signals, setSignals] = useState<Signal[]>([])
  const [signalsLoading, setSignalsLoading] = useState(false)
  const [executingSignalId, setExecutingSignalId] = useState<number | null>(null)

  // Balance
  const [balance, setBalance] = useState<any>(null)

  // ─── Simulation State ───
  const [simAccount, setSimAccount] = useState<SimAccount | null>(null)
  const [simPositions, setSimPositions] = useState<SimPosition[]>([])
  const [simOrders, setSimOrders] = useState<SimOrder[]>([])
  const [simLoading, setSimLoading] = useState(false)
  const [showAddFunds, setShowAddFunds] = useState(false)
  const [addFundsAmount, setAddFundsAmount] = useState('')
  const [showSimSettings, setShowSimSettings] = useState(false)
  const [showLiveSettings, setShowLiveSettings] = useState(false)
  const [autoTradeRunning, setAutoTradeRunning] = useState(false)
  const [simTab, setSimTab] = useState<'positions' | 'closed' | 'orders'>('positions')
  const [closedPositions, setClosedPositions] = useState<SimPosition[]>([])
  const [autoTradeMaxPositionsInput, setAutoTradeMaxPositionsInput] = useState('5')
  const [autoTradeRiskPctInput, setAutoTradeRiskPctInput] = useState('3')

  // ─── Live Futures State ───
  interface LivePosition {
    symbol: string
    side: string
    amount: number
    entry_price: number
    current_price: number
    unrealized_pnl: number
    unrealized_roe_pct: number
    leverage: string
    margin_mode: string
    margin_size: number
    initial_margin: number
    liquidation_price: number
    stop_loss: number | null
    take_profit: number | null
  }
  const [liveAccount, setLiveAccount] = useState<{
    balance: number; equity: number; unrealized_pnl: number;
    open_positions_count: number; open_positions: LivePosition[];
    reserved_margin: number; mmr: number; maintenance_margin: number; total_pnl: number;
    total_trades: number; winning_trades: number; losing_trades: number;
    settings: {
      is_active?: boolean; auto_trade?: boolean; dry_run?: boolean;
      auto_trade_pairs?: string; auto_trade_timeframe?: string;
      auto_trade_max_positions?: number; auto_trade_risk_pct?: number;
      auto_trade_mode?: string; auto_trade_leverage?: number;
      auto_trade_margin_mode?: string; enable_ai?: boolean;
    };
  } | null>(null)
  const [liveLoading, setLiveLoading] = useState(false)
  interface LiveOrder {
    orderId: string
    symbol: string
    side: string
    orderType: string
    price: string
    size: string
    filledQty?: string
    leverage?: string
    marginMode?: string
    createTime?: string
    status?: string
    presetStopLossPrice?: string
    presetStopSurplusPrice?: string
    stopLoss?: string
    takeProfit?: string
  }
  const [liveOpenOrders, setLiveOpenOrders] = useState<LiveOrder[]>([])
  const [liveTab, setLiveTab] = useState<'positions' | 'closed' | 'orders' | 'history'>('positions')
  const [cancellingOrderId, setCancellingOrderId] = useState<string | null>(null)
  const [closingLiveSymbol, setClosingLiveSymbol] = useState<string | null>(null)

  // Inline SL/TP edit state for live positions
  const [editingSlTp, setEditingSlTp] = useState<{
    posKey: string; field: 'sl' | 'tp'; value: string
  } | null>(null)
  const [savingSlTp, setSavingSlTp] = useState(false)
  const [optimizingOrders, setOptimizingOrders] = useState(false)
  const [optimizeResult, setOptimizeResult] = useState<any>(null)
  const [optimizingPositions, setOptimizingPositions] = useState(false)
  const [posOptimizeResult, setPosOptimizeResult] = useState<any>(null)

  interface LiveClosedTrade {
    id: number
    symbol: string
    side: string
    trade_side: string
    order_type: string
    amount: number
    price: number
    average_price: number | null
    stop_loss: number | null
    take_profit: number | null
    margin_mode: string | null
    leverage: number | null
    pnl: number | null
    pnl_percentage: number | null
    status: string
    created_at: string | null
    closed_at: string | null
  }
  const [liveClosedTrades, setLiveClosedTrades] = useState<LiveClosedTrade[]>([])
  const [liveOrderHistory, setLiveOrderHistory] = useState<any[]>([])

  // ─── Live Auto-Trade State ───
  const [liveTradeSettings, setLiveTradeSettings] = useState<any>(null)
  const [liveAutoTradeRunning, setLiveAutoTradeRunning] = useState(false)
  const [liveMaxPositionsInput, setLiveMaxPositionsInput] = useState('')
  const [liveRiskPctInput, setLiveRiskPctInput] = useState('')
  const [liveMaxPosSizeInput, setLiveMaxPosSizeInput] = useState('')
  const [liveMaxExposureInput, setLiveMaxExposureInput] = useState('')
  const [liveMarginSizeInput, setLiveMarginSizeInput] = useState('')
  const [liveMinGapInput, setLiveMinGapInput] = useState('')
  const [liveSettingsDirty, setLiveSettingsDirty] = useState(false)

  // Pair search for auto-trade settings
  const [pairSearch, setPairSearch] = useState('')
  const [showPairDropdown, setShowPairDropdown] = useState(false)
  const pairDropdownRef = useRef<HTMLDivElement>(null)

  // All available pairs from Bitget (spot + futures) with delisting info
  interface AvailablePair {
    symbol: string
    baseCoin: string
    quoteCoin: string
    market: string
    status: string
    delisting_ts: number | null
    delisting_date: string | null
    minLever: number | null
    maxLever: number | null
    futures_status?: string
    futures_adjustment?: string
    maintain_time?: string
    limit_open_time?: string
  }
  const [availablePairs, setAvailablePairs] = useState<AvailablePair[]>([])
  const [availablePairsLoading, setAvailablePairsLoading] = useState(false)
  const availablePairsFetched = useRef(false)

  // Leverage limits per pair from Bitget
  const [leverageLimits, setLeverageLimits] = useState<Record<string, { min: number; max: number }>>({})

  // ─── Strategy & Pine Script Overlay State ───
  interface Strategy {
    id: number
    name: string
    description: string
    pairs: string[]
    timeframe: string
    indicators: any[]
    buy_threshold: number
    sell_threshold: number
    is_active: boolean
  }
  interface PineScriptItem {
    id: number
    name: string
    description: string
    strategy_id: number | null
    script_type: string
    code: string
    pairs: string[]
    is_active: boolean
  }
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [pineScripts, setPineScripts] = useState<PineScriptItem[]>([])
  // Multi-select overlay: ["strategy:1", "pine:3", ...]
  const [overlaySelections, setOverlaySelections] = useState<string[]>([])
  const [showOverlayDropdown, setShowOverlayDropdown] = useState(false)
  const overlayDropdownRef = useRef<HTMLDivElement>(null)
  const [strategyOverlays, setStrategyOverlays] = useState<any[]>([])
  const [strategyMarkers, setStrategyMarkers] = useState<any[]>([])
  const [strategyEval, setStrategyEval] = useState<{
    name: string; score: number; action: string; indicator_values: any;
    eval_results?: { selection: string; name: string; score: number; action: string }[]
  } | null>(null)
  const [strategyLoading, setStrategyLoading] = useState(false)

  // Chart maximize state
  const [chartMaximized, setChartMaximized] = useState(false)

  // Chart mode: 'custom' (lightweight-charts with overlays) or 'tradingview' (full TradingView widget with indicators)
  const [chartMode, setChartMode] = useState<'custom' | 'tradingview'>('tradingview')

  // TradingView widget studies (indicators) — users can add/remove
  const [tvStudies, setTvStudies] = useState<{ id: string; inputs?: Record<string, any> }[]>([
    { id: SMART_MONEY_CONCEPTS_STUDY_ID },
  ])

  // Fetch strategies and pine scripts on mount
  useEffect(() => {
    apiClient.getStrategies()
      .then(res => {
        if (Array.isArray(res.data)) setStrategies(res.data)
      })
      .catch(() => {})
    apiClient.getPineScripts()
      .then(res => {
        if (Array.isArray(res.data)) setPineScripts(res.data)
      })
      .catch(() => {})
  }, [])

  // Toggle an overlay selection (multi-select)
  const toggleOverlaySelection = (value: string) => {
    setOverlaySelections(prev => {
      if (prev.includes(value)) {
        const next = prev.filter(v => v !== value)
        if (next.length === 0) {
          setStrategyOverlays([])
          setStrategyMarkers([])
          setStrategyEval(null)
        }
        // If removing a pine script, clear it from auto-trade settings
        if (value.startsWith('pine:') && simAccount?.auto_trade_pine_script_id === Number(value.split(':')[1])) {
          handleUpdateSettings({ auto_trade_pine_script_id: 0 })
        }
        return next
      } else {
        const next = [...prev, value]
        // Sync the first pine script to auto-trade settings
        const pineSelections = next.filter(v => v.startsWith('pine:'))
        if (pineSelections.length > 0) {
          const pineId = Number(pineSelections[0].split(':')[1])
          handleUpdateSettings({ auto_trade_pine_script_id: pineId })
        }
        return next
      }
    })
  }

  // Close overlay dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (overlayDropdownRef.current && !overlayDropdownRef.current.contains(e.target as Node)) {
        setShowOverlayDropdown(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Evaluate all selected overlays using multi-evaluate endpoint
  useEffect(() => {
    if (overlaySelections.length === 0 || !selectedSymbol) {
      setStrategyOverlays([])
      setStrategyMarkers([])
      setStrategyEval(null)
      return
    }
    setStrategyLoading(true)

    if (overlaySelections.length === 1) {
      // Single selection: use direct endpoint for efficiency
      const sel = overlaySelections[0]
      const evalData = {
        symbol: selectedSymbol,
        timeframe: selectedTimeframe,
        exchange: selectedExchange,
        limit: 200,
      }
      const promise = sel.startsWith('strategy:')
        ? apiClient.evaluateStrategy(Number(sel.split(':')[1]), evalData)
        : apiClient.evaluatePineScript(Number(sel.split(':')[1]), evalData)

      promise
        .then(res => {
          const data = res.data
          setStrategyOverlays(data.overlay_series || [])
          setStrategyMarkers(data.markers || [])
          setStrategyEval({
            name: data.pine_script_name || data.strategy_name,
            score: data.score,
            action: data.action,
            indicator_values: data.indicator_values,
          })
        })
        .catch(err => {
          const status = err?.response?.status
          const detail = err?.response?.data?.detail
          // Keep this non-fatal in dev: some Pine scripts are not extractable by backend parser.
          if (status === 400 && typeof detail === 'string' && detail.includes('Could not extract any indicators')) {
            console.warn(`[Overlay eval skipped] ${detail}`)
            if (sel.startsWith('pine:')) {
              const pineId = Number(sel.split(':')[1])
              setOverlaySelections(prev => prev.filter(v => v !== sel))
              if (simAccount?.auto_trade_pine_script_id === pineId) {
                void apiClient.updateSimSettings({ auto_trade_pine_script_id: 0 }).catch(() => {})
              }
            }
          } else {
            console.warn('[Overlay eval failed]', { status, detail })
          }
          setStrategyOverlays([])
          setStrategyMarkers([])
          setStrategyEval(null)
        })
        .finally(() => setStrategyLoading(false))
    } else {
      // Multiple selections: use multi-evaluate endpoint
      apiClient.evaluateMulti(overlaySelections, {
        symbol: selectedSymbol,
        timeframe: selectedTimeframe,
        exchange: selectedExchange,
        limit: 200,
      })
        .then(res => {
          const data = res.data
          setStrategyOverlays(data.overlay_series || [])
          setStrategyMarkers(data.markers || [])
          setStrategyEval({
            name: `Multi (${overlaySelections.length})`,
            score: data.score,
            action: data.action,
            indicator_values: data.indicator_values,
            eval_results: data.eval_results,
          })
        })
        .catch(err => {
          const status = err?.response?.status
          const detail = err?.response?.data?.detail
          console.warn('[Multi-eval failed]', { status, detail })
          setStrategyOverlays([])
          setStrategyMarkers([])
          setStrategyEval(null)
        })
        .finally(() => setStrategyLoading(false))
    }
  }, [overlaySelections, selectedSymbol, selectedTimeframe, selectedExchange])

  // Restore chart overlay from saved auto_trade_pine_script_id on load
  useEffect(() => {
    if (simAccount?.auto_trade_pine_script_id && pineScripts.length > 0 && overlaySelections.length === 0) {
      const saved = pineScripts.find(ps => ps.id === simAccount.auto_trade_pine_script_id)
      if (saved) {
        setOverlaySelections([`pine:${saved.id}`])
      }
    }
  }, [simAccount?.auto_trade_pine_script_id, pineScripts])

  // Fetch leverage limits on mount + when futures mode is selected 
  useEffect(() => {
    apiClient.getLeverageLimits()
      .then(res => {
        if (res.data?.limits) setLeverageLimits(res.data.limits)
      })
      .catch(() => {})
  }, [])

  // Reset leverage to pair's max when pair changes in futures mode
  useEffect(() => {
    if (orderTradeType === 'futures') {
      const limits = leverageLimits[selectedSymbol]
      if (limits) {
        // Set leverage to the pair's max (user can lower it)
        setOrderLeverage(limits.max)
      }
    }
  }, [selectedSymbol, leverageLimits, orderTradeType])

  // Fetch all available pairs from Bitget (spot + futures) with delisting info
  useEffect(() => {
    if (availablePairsFetched.current) return
    availablePairsFetched.current = true
    setAvailablePairsLoading(true)
    apiClient.getBitgetAvailablePairs('USDT')
      .then(res => {
        if (res.data?.pairs) {
          setAvailablePairs(res.data.pairs)
        }
      })
      .catch(() => {})
      .finally(() => setAvailablePairsLoading(false))
  }, [])

  useEffect(() => {
    setMounted(true)
    try {
      const saved = localStorage.getItem('tradebot_configured_pairs')
      if (saved) {
        const parsed = JSON.parse(saved)
        if (Array.isArray(parsed) && parsed.length > 0) {
          setConfiguredPairs([...new Set([...parsed, ...DEFAULT_PAIRS])])
        }
      }
    } catch { /* ignore */ }
  }, [])

  // Close chart pair dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (chartPairRef.current && !chartPairRef.current.contains(e.target as Node)) {
        setChartPairOpen(false)
        setChartPairQuery('')
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // ─── Simulation Data Fetchers ───
  const fetchSimAccount = useCallback(async () => {
    try {
      const res = await apiClient.getSimAccount()
      setSimAccount(res.data)
    } catch { setSimAccount(null) }
  }, [])

  const fetchSimPositions = useCallback(async () => {
    try {
      const res = await apiClient.getSimPositions('open')
      setSimPositions(Array.isArray(res.data) ? res.data : res.data?.positions || [])
    } catch { setSimPositions([]) }
  }, [])

  const fetchSimOrders = useCallback(async () => {
    try {
      const res = await apiClient.getSimOrders(30)
      setSimOrders(Array.isArray(res.data) ? res.data : res.data?.orders || [])
    } catch { setSimOrders([]) }
  }, [])

  const fetchClosedPositions = useCallback(async () => {
    try {
      const res = await apiClient.getSimPositions('closed')
      setClosedPositions(Array.isArray(res.data) ? res.data : res.data?.positions || [])
    } catch { setClosedPositions([]) }
  }, [])

  const refreshSimData = useCallback(async () => {
    await Promise.all([fetchSimAccount(), fetchSimPositions(), fetchSimOrders(), fetchClosedPositions()])
  }, [fetchSimAccount, fetchSimPositions, fetchSimOrders, fetchClosedPositions])

  useEffect(() => {
    if (!simAccount) return
    setAutoTradeMaxPositionsInput(String(simAccount.auto_trade_max_positions ?? 5))
    setAutoTradeRiskPctInput(String(simAccount.auto_trade_risk_pct ?? 3))
  }, [simAccount?.auto_trade_max_positions, simAccount?.auto_trade_risk_pct])

  // ─── Live Futures Data Fetcher ───
  const fetchLiveAccount = useCallback(async () => {
    setLiveLoading(true)
    try {
      const res = await apiClient.getBitgetFuturesAccountSummary()
      setLiveAccount(res.data)
    } catch { setLiveAccount(null) }
    finally { setLiveLoading(false) }
  }, [])

  const fetchLiveOpenOrders = useCallback(async () => {
    try {
      const res = await apiClient.getBitgetFuturesOpenOrders()
      const orders = res.data?.orders || []
      setLiveOpenOrders(Array.isArray(orders) ? orders : [])
    } catch { setLiveOpenOrders([]) }
  }, [])

  const cancelLiveOrder = async (order: LiveOrder) => {
    setCancellingOrderId(order.orderId)
    try {
      await apiClient.cancelBitgetFuturesOrder(
        order.orderId,
        order.symbol,
        'USDT',
        'USDT-FUTURES'
      )
      setOrderResult({ success: true, message: `Order ${order.orderId} cancelled` })
      fetchLiveOpenOrders()
      fetchLiveAccount()
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Cancel failed' })
    } finally { setCancellingOrderId(null) }
  }

  const fetchLiveClosedTrades = useCallback(async () => {
    try {
      const res = await apiClient.getLiveTradeHistory(100)
      setLiveClosedTrades(res.data?.trades || [])
    } catch { setLiveClosedTrades([]) }
  }, [])

  const fetchLiveOrderHistory = useCallback(async () => {
    try {
      const res = await apiClient.getBitgetFuturesOrderHistory()
      setLiveOrderHistory(res.data?.orders || [])
    } catch { setLiveOrderHistory([]) }
  }, [])

  const closeLivePosition = async (symbol: string, side: string) => {
    setClosingLiveSymbol(`${symbol}-${side}`)
    try {
      const res = await apiClient.closeLivePosition({ symbol, side })
      const pnl = res.data?.pnl ?? 0
      setOrderResult({
        success: true,
        message: `${symbol} ${side} closed — PnL: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`,
      })
      await refreshLiveData()
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to close position' })
    } finally { setClosingLiveSymbol(null) }
  }

  const closeAllLivePositions = async () => {
    setLiveLoading(true)
    try {
      const res = await apiClient.closeAllLivePositions()
      const closed = res.data?.closed ?? 0
      const totalPnl = res.data?.total_pnl ?? 0
      setOrderResult({
        success: true,
        message: `Closed ${closed} positions — Total PnL: ${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`,
      })
      await refreshLiveData()
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to close all positions' })
    } finally { setLiveLoading(false) }
  }

  const refreshLiveData = useCallback(async () => {
    await Promise.all([fetchLiveAccount(), fetchLiveOpenOrders(), fetchLiveClosedTrades()])
  }, [fetchLiveAccount, fetchLiveOpenOrders, fetchLiveClosedTrades])

  // Handle SL/TP changes from chart drag
  const handleChartSlTpChange = useCallback(async (
    symbol: string, side: string, sl: number | null, tp: number | null
  ) => {
    const isLive = tradingMode === 'live'
    if (isLive) {
      try {
        await apiClient.updateLiveSlTp({
          symbol,
          side,
          stop_loss: sl ?? undefined,
          take_profit: tp ?? undefined,
        })
        setOrderResult({ success: true, message: `SL/TP updated for ${symbol} ${side}` })
        await refreshLiveData()
      } catch (err: any) {
        setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to update SL/TP' })
      }
    }
    // Sim positions don't have a direct update endpoint — handled by backfill
  }, [tradingMode, refreshLiveData])

  // Save inline SL/TP edit from position table
  const saveInlineSlTp = useCallback(async (symbol: string, side: string, field: 'sl' | 'tp', value: string) => {
    const numVal = parseFloat(value)
    if (isNaN(numVal) || numVal <= 0) {
      setEditingSlTp(null)
      return
    }
    setSavingSlTp(true)
    try {
      await apiClient.updateLiveSlTp({
        symbol: symbol.replace(/USDT$/, '/USDT'),
        side,
        stop_loss: field === 'sl' ? numVal : undefined,
        take_profit: field === 'tp' ? numVal : undefined,
      })
      setOrderResult({ success: true, message: `${field === 'sl' ? 'SL' : 'TP'} updated for ${symbol}` })
      await refreshLiveData()
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to update SL/TP' })
    } finally {
      setSavingSlTp(false)
      setEditingSlTp(null)
    }
  }, [refreshLiveData])

  // AI-optimize pending limit orders for better entries
  const handleOptimizeLimitOrders = useCallback(async () => {
    setOptimizingOrders(true)
    setOptimizeResult(null)
    try {
      const res = await apiClient.optimizeLimitOrders()
      const data = res.data
      setOptimizeResult(data)
      const adjusted = data.orders_adjusted || 0
      const reviewed = data.orders_reviewed || 0
      if (adjusted > 0) {
        setOrderResult({ success: true, message: `AI optimized ${adjusted}/${reviewed} limit order(s) for better entry` })
        await refreshLiveData()
      } else if (reviewed > 0) {
        setOrderResult({ success: true, message: `AI reviewed ${reviewed} order(s) — entries are optimal` })
      } else {
        setOrderResult({ success: true, message: data.reason || 'No limit orders to optimize' })
      }
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to optimize orders' })
    } finally {
      setOptimizingOrders(false)
    }
  }, [refreshLiveData])

  // AI-optimize SL/TP for open filled positions
  const handleOptimizePositions = useCallback(async () => {
    setOptimizingPositions(true)
    setPosOptimizeResult(null)
    try {
      const res = await apiClient.optimizeOpenPositions()
      const data = res.data
      setPosOptimizeResult(data)
      const adjusted = data.positions_adjusted || 0
      const reviewed = data.positions_reviewed || 0
      if (adjusted > 0) {
        setOrderResult({ success: true, message: `AI recalculated SL/TP for ${adjusted}/${reviewed} position(s)` })
        await refreshLiveData()
      } else if (reviewed > 0) {
        setOrderResult({ success: true, message: `AI reviewed ${reviewed} position(s) — SL/TP levels are optimal` })
      } else {
        setOrderResult({ success: true, message: data.reason || 'No open positions to optimize' })
      }
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to optimize positions' })
    } finally {
      setOptimizingPositions(false)
    }
  }, [refreshLiveData])

  // Fetch ticker when symbol or exchange changes
  const fetchTicker = useCallback(async () => {
    setTickerLoading(true)
    try {
      const res = await apiClient.getTicker(selectedExchange, selectedSymbol)
      const t = res.data?.ticker || res.data
      setTicker(t)
    } catch (err) {
      console.error('Ticker fetch failed:', err)
      setTicker(null)
    } finally {
      setTickerLoading(false)
    }
  }, [selectedSymbol, selectedExchange])

  // Fetch balance
  const fetchBalance = useCallback(async () => {
    try {
      const quoteCurrency = selectedSymbol.split('/')[1] || 'USDT'
      const res = await apiClient.getBalance(selectedExchange, quoteCurrency)
      setBalance(res.data)
    } catch { setBalance(null) }
  }, [selectedExchange, selectedSymbol])

  // Fetch actionable signals for selected pair
  const fetchSignals = useCallback(async () => {
    setSignalsLoading(true)
    try {
      const res = await apiClient.getSignals({ symbol: selectedSymbol, limit: 10 })
      setSignals(res.data)
    } catch { setSignals([]) }
    finally { setSignalsLoading(false) }
  }, [selectedSymbol])

  useEffect(() => {
    if (!mounted) return
    fetchTicker()
    fetchBalance()
    fetchSignals()
    fetchSimAccount()
    const interval = setInterval(() => { fetchTicker(); fetchSignals() }, 15000)
    return () => clearInterval(interval)
  }, [mounted, fetchTicker, fetchBalance, fetchSignals, fetchSimAccount])

  // If sim is active, periodically refresh sim data (10s for real-time PnL)
  useEffect(() => {
    if (!mounted || !simAccount?.is_active) return
    refreshSimData()
    const interval = setInterval(refreshSimData, 10000)
    return () => clearInterval(interval)
  }, [mounted, simAccount?.is_active, refreshSimData])

  // If live mode, periodically refresh live futures data
  useEffect(() => {
    if (!mounted || tradingMode !== 'live') return
    refreshLiveData()
    const interval = setInterval(refreshLiveData, 10000)
    return () => clearInterval(interval)
  }, [mounted, tradingMode, refreshLiveData])

  // When ticker updates, sync limit price
  useEffect(() => {
    if (ticker && orderType === 'limit' && !orderPrice) {
      setOrderPrice(ticker.last.toString())
    }
  }, [ticker, orderType, orderPrice])

  // ─── Simulation Actions ───
  const toggleSimulation = async () => {
    setSimLoading(true)
    try {
      const newActive = !simAccount?.is_active
      await apiClient.toggleSimAccount(newActive)
      await fetchSimAccount()
      if (newActive) {
        setTradingMode('sim')
        refreshSimData()
      }
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to toggle simulation' })
    } finally { setSimLoading(false) }
  }

  const handleAddFunds = async () => {
    const amt = Number(addFundsAmount)
    if (!amt || amt <= 0) return
    setSimLoading(true)
    try {
      await apiClient.addSimFunds(amt)
      await fetchSimAccount()
      setAddFundsAmount('')
      setShowAddFunds(false)
      setOrderResult({ success: true, message: `Added $${amt.toLocaleString()} to simulation account` })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to add funds' })
    } finally { setSimLoading(false) }
  }

  const handleResetSim = async () => {
    if (!confirm('Reset simulation account? All positions and orders will be cleared.')) return
    setSimLoading(true)
    try {
      await apiClient.resetSimAccount(10000)
      await refreshSimData()
      setOrderResult({ success: true, message: 'Simulation account reset to $10,000' })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to reset' })
    } finally { setSimLoading(false) }
  }

  const handleUpdateSettings = async (settings: any) => {
    setSimLoading(true)
    try {
      await apiClient.updateSimSettings(settings)
      await fetchSimAccount()
      setOrderResult({ success: true, message: 'Simulation settings updated' })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to update settings' })
    } finally { setSimLoading(false) }
  }

  const runAutoTradeCycle = async () => {
    try {
      const res = await apiClient.runAutoTradeCycle()
      await refreshSimData()
      const data = res.data
      const msg = data?.orders_placed
        ? `Auto-trade cycle: ${data.orders_placed} orders placed, ${data.sl_tp_closed || 0} SL/TP hit`
        : 'Auto-trade cycle complete — no new orders'
      setOrderResult({ success: true, message: msg })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Auto-trade cycle failed' })
    }
  }

  const cancelSimOrder = async (orderId: number) => {
    try {
      setSimLoading(true)
      const res = await apiClient.cancelSimOrder(orderId)
      await refreshSimData()
      setOrderResult({ success: true, message: `Order #${orderId} canceled — $${res.data?.refunded?.toFixed(2) || '0'} refunded` })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to cancel order' })
    } finally { setSimLoading(false) }
  }

  const [closingPositionId, setClosingPositionId] = useState<number | null>(null)

  const closeSimPosition = async (positionId: number, symbol: string) => {
    try {
      setClosingPositionId(positionId)
      const res = await apiClient.closeSimPosition(positionId)
      await refreshSimData()
      const pnl = res.data?.pnl ?? 0
      setOrderResult({ success: true, message: `${symbol} closed — PnL: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}, $${res.data?.refunded?.toFixed(2) || '0'} refunded` })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to close position' })
    } finally { setClosingPositionId(null) }
  }

  const closeAllSimPositions = async () => {
    try {
      setSimLoading(true)
      const res = await apiClient.closeAllSimPositions()
      await refreshSimData()
      setOrderResult({ success: true, message: `Closed ${res.data?.closed ?? 0} positions — Balance: $${res.data?.balance?.toFixed(2) || '?'}` })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to close all positions' })
    } finally { setSimLoading(false) }
  }

  const toggleAutoTradeLoop = async () => {
    try {
      if (autoTradeRunning) {
        await apiClient.stopAutoTradeLoop()
        setAutoTradeRunning(false)
        setOrderResult({ success: true, message: 'Auto-trade loop stopped' })
      } else {
        await apiClient.startAutoTradeLoop(60)
        setAutoTradeRunning(true)
        setOrderResult({ success: true, message: 'Auto-trade loop started (runs every 60s)' })
      }
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to toggle auto-trade loop' })
    }
  }

  // Fetch auto-trade loop status from backend on mount (survives page refresh)
  useEffect(() => {
    const checkLoopStatus = async () => {
      try {
        const res = await apiClient.getAutoTradeLoopStatus()
        setAutoTradeRunning(res.data?.running ?? false)
      } catch {}
    }
    checkLoopStatus()
  }, [])

  // ── Live Auto-Trade Handlers ──
  const fetchLiveTradeSettings = async () => {
    try {
      const res = await apiClient.getLiveTradeSettings()
      setLiveTradeSettings(res.data)
    } catch {}
  }

  const handleUpdateLiveSettings = async (settings: any) => {
    try {
      const res = await apiClient.updateLiveTradeSettings(settings)
      setLiveTradeSettings(res.data)
      // Force-sync inputs to the confirmed DB values
      if (res.data) {
        setLiveMaxPositionsInput(String(res.data.auto_trade_max_positions ?? 3))
        setLiveRiskPctInput(String(res.data.auto_trade_risk_pct ?? 1))
        setLiveMaxPosSizeInput(String(res.data.max_position_size_usdt ?? 500))
        setLiveMaxExposureInput(String(res.data.max_total_exposure_usdt ?? 5000))
      }
      setOrderResult({ success: true, message: 'Live trade settings updated' })
    } catch (err: any) {
      // On error, re-fetch to restore correct values
      fetchLiveTradeSettings()
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to update live settings' })
    }
  }

  const runLiveAutoTradeCycle = async () => {
    try {
      const res = await apiClient.runLiveAutoTradeCycle()
      await refreshLiveData()
      await fetchLiveTradeSettings()
      const data = res.data
      const isDryRun = Boolean(data?.dry_run)
      const msg = data?.orders_placed?.length
        ? `${isDryRun ? '[LIVE DRY-RUN]' : '[LIVE]'} Auto-trade cycle: ${data.orders_placed.length} ${isDryRun ? 'orders planned' : 'orders placed'}`
        : `${isDryRun ? '[LIVE DRY-RUN]' : '[LIVE]'} Auto-trade cycle complete — no new orders`
      setOrderResult({ success: true, message: msg })
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Live auto-trade cycle failed' })
    }
  }

  const toggleLiveAutoTradeLoop = async () => {
    try {
      if (liveAutoTradeRunning) {
        await apiClient.stopLiveAutoTradeLoop()
        setLiveAutoTradeRunning(false)
        setOrderResult({ success: true, message: '[LIVE] Auto-trade loop stopped' })
      } else {
        await apiClient.startLiveAutoTradeLoop(60)
        setLiveAutoTradeRunning(true)
        setOrderResult({
          success: true,
          message: `${liveTradeSettings?.dry_run ? '[LIVE DRY-RUN]' : '[LIVE]'} Auto-trade loop started (runs every 60s)`,
        })
      }
    } catch (err: any) {
      setOrderResult({ success: false, message: err?.response?.data?.detail || 'Failed to toggle live auto-trade loop' })
    }
  }

  // Fetch live trade settings + loop status on mount and periodically
  useEffect(() => {
    fetchLiveTradeSettings()
    const checkLiveLoop = async () => {
      try {
        const res = await apiClient.getLiveAutoTradeLoopStatus()
        setLiveAutoTradeRunning(res.data?.running ?? false)
      } catch {}
    }
    checkLiveLoop()
    // Refresh settings + loop status every 15s so values stay in sync after refresh
    const iv = setInterval(() => {
      fetchLiveTradeSettings()
      checkLiveLoop()
    }, 15000)
    return () => clearInterval(iv)
  }, [])

  // Sync live settings inputs when data loads (only if inputs are empty or stale)
  useEffect(() => {
    if (!liveTradeSettings) return
    const mp = String(liveTradeSettings.auto_trade_max_positions ?? 3)
    const rp = String(liveTradeSettings.auto_trade_risk_pct ?? 1)
    const ps = String(liveTradeSettings.max_position_size_usdt ?? 500)
    const ex = String(liveTradeSettings.max_total_exposure_usdt ?? 5000)
    const ms = String(liveTradeSettings.margin_size_usdt ?? 10)
    const mg = String(liveTradeSettings.min_entry_gap_pct ?? 2)
    // Only overwrite if input is empty (initial) or matches the previous DB value
    setLiveMaxPositionsInput(prev => prev === '' || prev === mp ? mp : prev)
    setLiveRiskPctInput(prev => prev === '' || prev === rp ? rp : prev)
    setLiveMaxPosSizeInput(prev => prev === '' || prev === ps ? ps : prev)
    setLiveMaxExposureInput(prev => prev === '' || prev === ex ? ex : prev)
    setLiveMarginSizeInput(prev => prev === '' || prev === ms ? ms : prev)
    setLiveMinGapInput(prev => prev === '' || prev === mg ? mg : prev)
  }, [liveTradeSettings])

  // Close pair dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (pairDropdownRef.current && !pairDropdownRef.current.contains(e.target as Node)) {
        setShowPairDropdown(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Auto-dismiss order result toast after 6 seconds
  useEffect(() => {
    if (!orderResult) return
    const timer = setTimeout(() => setOrderResult(null), 6000)
    return () => clearTimeout(timer)
  }, [orderResult])

  // Place manual order — routes through sim or real (futures-first for live)
  const placeOrder = async () => {
    if (!orderAmount || Number(orderAmount) <= 0) return
    setOrderLoading(true)
    setOrderResult(null)

    if (tradingMode === 'sim' && simAccount?.is_active) {
      // Simulation order
      try {
        const res = await apiClient.placeSimOrder({
          symbol: selectedSymbol,
          side: orderSide,
          amount: Number(orderAmount),
          amount_mode: orderAmountMode,
          price: orderType === 'limit' ? Number(orderPrice) : undefined,
          order_type: orderType,
          auto_sl: true,
          trade_type: orderTradeType,
          leverage: orderTradeType === 'futures' ? orderLeverage : undefined,
          margin_mode: orderTradeType === 'futures' ? orderMarginMode : undefined,
        })
        const amtLabel = orderAmountMode === 'quote'
          ? `$${orderAmount} USDT worth of ${selectedSymbol.split('/')[0]}`
          : `${orderAmount} ${selectedSymbol.split('/')[0]}`
        setOrderResult({
          success: true,
          message: `[SIM] ${orderSide.toUpperCase()} order placed for ${amtLabel}`,
          data: res.data,
        })
        setOrderAmount('')
        refreshSimData()
      } catch (err: any) {
        setOrderResult({
          success: false,
          message: err?.response?.data?.detail || err.message || 'Sim order failed',
        })
      } finally { setOrderLoading(false) }
      return
    }

    // Live order — use futures by default
    try {
      if (orderTradeType === 'futures') {
        const symbolClean = selectedSymbol.replace('/', '')
        const res = await apiClient.createBitgetFuturesOrder({
          symbol: symbolClean,
          margin_coin: 'USDT',
          side: orderSide === 'buy' ? 'buy' : 'sell',
          order_type: orderType,
          size: String(Number(orderAmount)),
          price: orderType === 'limit' ? String(Number(orderPrice)) : undefined,
          margin_mode: orderMarginMode,
          leverage: orderLeverage,
          trade_side: 'open',
          product_type: 'USDT-FUTURES',
          stop_loss_pct: orderAutoSlTp && orderSlPct ? Number(orderSlPct) : undefined,
          take_profit_pct: orderAutoSlTp && orderTpPct ? Number(orderTpPct) : undefined,
        })
        setOrderResult({
          success: true,
          message: `[LIVE FUTURES] ${orderSide.toUpperCase()} ${selectedSymbol} order placed`,
          data: res.data,
        })
        setOrderAmount('')
        fetchLiveAccount()
      } else {
        const res = await apiClient.placeSpotOrder({
          exchange: selectedExchange,
          symbol: selectedSymbol,
          side: orderSide,
          order_type: orderType,
          amount: Number(orderAmount),
          price: orderType === 'limit' ? Number(orderPrice) : undefined,
        })
        setOrderResult({
          success: true,
          message: `[LIVE SPOT] ${orderSide.toUpperCase()} ${selectedSymbol} order placed`,
          data: res.data,
        })
        setOrderAmount('')
        fetchBalance()
      }
    } catch (err: any) {
      setOrderResult({
        success: false,
        message: err?.response?.data?.detail || err.message || 'Order failed',
      })
    } finally {
      setOrderLoading(false)
    }
  }

  // Execute trade from a signal
  const executeSignal = async (signal: Signal) => {
    setExecutingSignalId(signal.id)
    setOrderResult(null)

    if (tradingMode === 'sim' && simAccount?.is_active) {
      // Execute signal through simulation
      try {
        const riskAmount = simAccount.balance * (simAccount.auto_trade_risk_pct / 100)
        const signalPrice = signal.price || ticker?.last || 1
        const amountMode = simAccount.auto_trade_amount_mode || 'quote'
        const amount = amountMode === 'quote' ? riskAmount : riskAmount / signalPrice
        const tradeType = simAccount.auto_trade_mode || 'futures'
        const res = await apiClient.placeSimOrder({
          symbol: signal.symbol,
          side: signal.action.toLowerCase() as 'buy' | 'sell',
          amount,
          amount_mode: amountMode,
          price: signal.price || undefined,
          signal_id: signal.id,
          auto_sl: true,
          trade_type: tradeType,
          leverage: tradeType === 'futures' ? (simAccount.auto_trade_leverage || 10) : undefined,
          margin_mode: tradeType === 'futures' ? (simAccount.auto_trade_margin_mode || 'crossed') : undefined,
        })
        setOrderResult({
          success: true,
          message: `[SIM ${tradeType.toUpperCase()}] ${signal.action.toUpperCase()} ${signal.symbol} executed from signal`,
          data: res.data,
        })
        refreshSimData()
        fetchSignals()
      } catch (err: any) {
        const detail = err?.response?.data?.detail
        const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ') : (detail?.msg || err.message || 'Sim execution failed')
        setOrderResult({
          success: false,
          message: msg,
        })
      } finally { setExecutingSignalId(null) }
      return
    }

    // Live execution — use backend endpoint (same logic as auto_trade_cycle)
    try {
      const res = await apiClient.executeLiveSignal(signal.id)
      setOrderResult({
        success: true,
        message: `[LIVE ${res.data.order_type?.toUpperCase() || 'FUTURES'}] ${signal.action.toUpperCase()} ${signal.symbol} @ ${res.data.leverage}x leverage | ${res.data.order_type} | SL: ${res.data.sl ? formatPrice(Number(res.data.sl), false) : 'N/A'} | TP: ${res.data.tp ? formatPrice(Number(res.data.tp), false) : 'N/A'}${res.data.dry_run ? ' [DRY RUN]' : ''}`,
        data: res.data,
      })
      fetchSignals()
      fetchLiveAccount()
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d: any) => d.msg || JSON.stringify(d)).join('; ') : (detail?.msg || err.message || 'Live execution failed')
      setOrderResult({
        success: false,
        message: msg,
      })
    } finally {
      setExecutingSignalId(null)
    }
  }

  const estimatedTotal = () => {
    const amt = Number(orderAmount) || 0
    const px = orderType === 'limit' ? (Number(orderPrice) || 0) : (ticker?.last || 0)
    if (orderAmountMode === 'quote') {
      // Amount is in USDT — show equivalent base qty
      return px > 0 ? (amt / px).toFixed(6) : '0.000000'
    }
    return (amt * px).toFixed(2)
  }

  const winRate = simAccount && simAccount.total_trades > 0
    ? ((simAccount.winning_trades / simAccount.total_trades) * 100).toFixed(1)
    : '0.0'

  if (!mounted) return null

  const simActive = simAccount?.is_active ?? false
  const isSimMode = tradingMode === 'sim'
  const isLiveMode = tradingMode === 'live'

  return (
    <>
      <Head><title>TradeBot - Trading{isSimMode ? ' [SIM]' : isLiveMode ? ' [LIVE]' : ''}</title></Head>

      <div className="space-y-5 max-w-7xl mx-auto">
        {/* Header with Mode Switcher */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white flex items-center gap-3">
              Trading
              {isSimMode && simActive && (
                <span className="text-sm font-medium bg-purple-500/20 border border-purple-500/40 text-purple-300 px-3 py-1 rounded-full">
                  SIM MODE
                </span>
              )}
              {isLiveMode && (
                <span className="text-sm font-medium bg-green-500/20 border border-green-500/40 text-green-300 px-3 py-1 rounded-full">
                  LIVE MODE
                </span>
              )}
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              {isSimMode && simActive
                ? 'Simulation mode active — orders are virtual with smart stop-loss'
                : isLiveMode
                  ? 'Live trading — orders execute on exchange with real funds'
                  : 'Place orders manually or execute signals for configured pairs'}
            </p>
          </div>

          {/* Mode Switcher */}
          <div className="flex items-center gap-2">
            <div className="flex bg-gray-900 rounded-lg border border-gray-700 overflow-hidden">
              <button
                onClick={() => {
                  setTradingMode('sim')
                  if (!simActive) toggleSimulation()
                }}
                className={`flex items-center gap-1.5 px-4 py-2 text-sm font-semibold transition ${
                  isSimMode
                    ? 'bg-purple-600 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800'
                }`}
              >
                <Shield className="w-4 h-4" />
                Sim
              </button>
              <button
                onClick={() => {
                  setTradingMode('live')
                  refreshLiveData()
                }}
                className={`flex items-center gap-1.5 px-4 py-2 text-sm font-semibold transition ${
                  isLiveMode
                    ? 'bg-green-600 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800'
                }`}
              >
                <Zap className="w-4 h-4" />
                Live
              </button>
            </div>
          </div>
        </div>

        {/* ─── Dashboard (Sim or Live) ─── */}
        {isSimMode && simActive && simAccount && (
          <div className="bg-gradient-to-r from-purple-900/20 to-blue-900/20 border border-purple-500/30 rounded-lg p-4">
            <div className="flex flex-wrap items-center justify-between gap-4">
              {/* Account Stats */}
              <div className="flex flex-wrap gap-6">
                <div>
                  <span className="text-xs text-gray-400 block">Available Balance</span>
                  <span className="text-lg font-bold font-mono text-white">
                    ${simAccount.balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </span>
                  {toZar(simAccount.balance) && <span className="text-[10px] text-gray-500 block font-mono">{toZar(simAccount.balance)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Equity</span>
                  <span className="text-lg font-bold font-mono text-white">
                    ${simAccount.equity.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </span>
                  {toZar(simAccount.equity) && <span className="text-[10px] text-gray-500 block font-mono">{toZar(simAccount.equity)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Total P&L</span>
                  <span className={`text-lg font-bold font-mono ${
                    simAccount.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                  }`}>
                    {simAccount.total_pnl >= 0 ? '+' : ''}${simAccount.total_pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </span>
                  {toZar(simAccount.total_pnl) && <span className={`text-[10px] block font-mono ${simAccount.total_pnl >= 0 ? 'text-green-700' : 'text-red-700'}`}>{toZar(simAccount.total_pnl)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Win Rate</span>
                  <span className="text-lg font-bold font-mono text-white">{winRate}%</span>
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Trades</span>
                  <span className="text-sm font-mono text-white">
                    <span className="text-green-400">{simAccount.winning_trades}W</span>
                    {' / '}
                    <span className="text-red-400">{simAccount.losing_trades}L</span>
                    {' / '}
                    {simAccount.total_trades} total
                  </span>
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Open Positions</span>
                  <span className="text-lg font-bold font-mono text-white">
                    {simAccount.open_positions_count}
                  </span>
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Unrealized P&L</span>
                  <span className={`text-lg font-bold font-mono ${
                    simAccount.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                  }`}>
                    {simAccount.unrealized_pnl >= 0 ? '+' : ''}${simAccount.unrealized_pnl.toFixed(2)}
                  </span>
                  {toZar(simAccount.unrealized_pnl) && <span className={`text-[10px] block font-mono ${simAccount.unrealized_pnl >= 0 ? 'text-green-700' : 'text-red-700'}`}>{toZar(simAccount.unrealized_pnl)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Reserved Margin</span>
                  <span className="text-lg font-bold font-mono text-orange-300">
                    ${simAccount.reserved_margin.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </span>
                  {toZar(simAccount.reserved_margin) && <span className="text-[10px] text-orange-700 block font-mono">{toZar(simAccount.reserved_margin)}</span>}
                </div>
              </div>

              {/* Active Settings Chips */}
              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-blue-500/15 border border-blue-500/30 text-blue-300 font-medium">
                  {simAccount.auto_trade_timeframe || '1h'}
                </span>
                <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                  simAccount.auto_trade_mode === 'futures'
                    ? 'bg-orange-500/15 border border-orange-500/30 text-orange-300'
                    : 'bg-blue-500/15 border border-blue-500/30 text-blue-300'
                }`}>
                  {(simAccount.auto_trade_mode || 'spot').toUpperCase()}
                  {simAccount.auto_trade_mode === 'futures' && ` ${simAccount.auto_trade_leverage}x`}
                </span>
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-purple-500/15 border border-purple-500/30 text-purple-300 font-medium">
                  Risk: {simAccount.auto_trade_risk_pct}%
                </span>
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-500/15 border border-gray-500/30 text-gray-300 font-medium">
                  Max: {simAccount.auto_trade_max_positions} pos
                </span>
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-500/15 border border-gray-500/30 text-gray-300 font-medium">
                  {(simAccount.auto_trade_pairs || []).length} pairs
                </span>
                {simAccount.auto_trade && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-green-500/15 border border-green-500/30 text-green-300 font-medium animate-pulse">
                    AUTO
                  </span>
                )}
              </div>

              {/* Sim Actions */}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowAddFunds(!showAddFunds)}
                  className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded bg-green-600/20 border border-green-500/30 text-green-300 hover:bg-green-600/30 transition"
                >
                  <Plus className="w-3 h-3" /> Add Funds
                </button>
                <button
                  onClick={() => setShowSimSettings(!showSimSettings)}
                  className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded bg-blue-600/20 border border-blue-500/30 text-blue-300 hover:bg-blue-600/30 transition"
                >
                  <Settings className="w-3 h-3" /> Settings
                </button>
                <button
                  onClick={handleResetSim}
                  className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded bg-gray-600/20 border border-gray-500/30 text-gray-300 hover:bg-gray-600/30 transition"
                >
                  <RotateCcw className="w-3 h-3" /> Reset
                </button>
              </div>
            </div>

            {/* Add Funds Inline */}
            {showAddFunds && (
              <div className="mt-3 flex items-center gap-2 border-t border-purple-500/20 pt-3">
                <Wallet className="w-4 h-4 text-green-400" />
                <input
                  type="number"
                  value={addFundsAmount}
                  onChange={e => setAddFundsAmount(e.target.value)}
                  placeholder="Amount (USDT)"
                  min="1"
                  step="any"
                  className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-sm text-white w-40 focus:border-green-500 outline-none"
                />
                {[100, 500, 1000, 5000].map(amt => (
                  <button
                    key={amt}
                    onClick={() => setAddFundsAmount(amt.toString())}
                    className="px-2 py-1 text-xs rounded bg-gray-800 text-gray-300 hover:bg-gray-700 border border-gray-700"
                  >
                    ${amt.toLocaleString()}
                  </button>
                ))}
                <button
                  onClick={handleAddFunds}
                  disabled={!addFundsAmount || Number(addFundsAmount) <= 0}
                  className="px-4 py-1.5 text-xs font-semibold rounded bg-green-600 text-white hover:bg-green-500 disabled:opacity-40 transition"
                >
                  Deposit
                </button>
              </div>
            )}

            {/* Auto-Trade Settings */}
            {showSimSettings && (
              <div className="mt-3 border-t border-purple-500/20 pt-3 space-y-3">
                <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                  <Target className="w-4 h-4 text-blue-400" /> Auto-Trade Settings
                </h4>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Auto-Trade</label>
                    <button
                      onClick={() => handleUpdateSettings({ auto_trade: !simAccount.auto_trade })}
                      className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                        simAccount.auto_trade
                          ? 'bg-green-600 text-white'
                          : 'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}
                    >
                      {simAccount.auto_trade ? 'Enabled' : 'Disabled'}
                    </button>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">AI Agents</label>
                    <button
                      onClick={() => handleUpdateSettings({ enable_ai: !simAccount.enable_ai })}
                      className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                        simAccount.enable_ai
                          ? 'bg-purple-600 text-white'
                          : 'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}
                    >
                      {simAccount.enable_ai ? 'AI On' : 'AI Off'}
                    </button>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Trade Mode</label>
                    <div className="flex gap-1">
                      {(['spot', 'futures'] as const).map(mode => (
                        <button
                          key={mode}
                          onClick={() => handleUpdateSettings({ auto_trade_mode: mode })}
                          className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                            (simAccount.auto_trade_mode || 'spot') === mode
                              ? mode === 'futures' ? 'bg-orange-600 text-white' : 'bg-blue-600 text-white'
                              : 'bg-gray-800 text-gray-400 border border-gray-700'
                          }`}
                        >
                          {mode.charAt(0).toUpperCase() + mode.slice(1)}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
                    <select
                      value={simAccount.auto_trade_timeframe}
                      onChange={e => handleUpdateSettings({ auto_trade_timeframe: e.target.value })}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    >
                      {timeframes.map(tf => (
                        <option key={tf.value} value={tf.value}>{tf.label}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Max Positions</label>
                    <input
                      type="number"
                      value={autoTradeMaxPositionsInput}
                      min="1"
                      max="100"
                      onChange={e => setAutoTradeMaxPositionsInput(e.target.value)}
                      onBlur={() => handleUpdateSettings({ auto_trade_max_positions: Number(autoTradeMaxPositionsInput || simAccount.auto_trade_max_positions || 1) })}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Risk per Trade %</label>
                    <input
                      type="number"
                      value={autoTradeRiskPctInput}
                      min="0.5"
                      max="10"
                      step="0.5"
                      onChange={e => setAutoTradeRiskPctInput(e.target.value)}
                      onBlur={() => handleUpdateSettings({ auto_trade_risk_pct: Number(autoTradeRiskPctInput || simAccount.auto_trade_risk_pct || 0.5) })}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Order Amount In</label>
                    <div className="flex bg-gray-900 rounded overflow-hidden border border-gray-700">
                      {(['quote', 'base'] as const).map(mode => (
                        <button
                          key={mode}
                          onClick={() => handleUpdateSettings({ auto_trade_amount_mode: mode })}
                          className={`flex-1 px-2 py-1.5 text-xs font-semibold transition ${
                            (simAccount.auto_trade_amount_mode || 'quote') === mode
                              ? 'bg-blue-600 text-white'
                              : 'text-gray-400 hover:text-gray-200'
                          }`}
                        >
                          {mode === 'quote' ? 'USDT' : 'Pair Qty'}
                        </button>
                      ))}
                    </div>
                    <p className="text-[10px] text-gray-500 mt-1">
                      {(simAccount.auto_trade_amount_mode || 'quote') === 'quote'
                        ? 'Risk % calculates USDT value, converts to pair quantity'
                        : 'Risk % calculates directly in base pair units'}
                    </p>
                  </div>
                </div>

                {/* Futures-specific settings */}
                {(simAccount.auto_trade_mode || 'spot') === 'futures' && (() => {
                  // Compute effective max leverage from selected pairs
                  const selectedPairs = simAccount.auto_trade_pairs || []
                  const pairMaxLevers = selectedPairs
                    .map(p => leverageLimits[p]?.max)
                    .filter((v): v is number => v != null)
                  // Use the lowest max across selected pairs (most restrictive), default 125
                  const effectiveMax = pairMaxLevers.length > 0 ? Math.min(...pairMaxLevers) : 125
                  const currentLev = Math.min(simAccount.auto_trade_leverage || 10, effectiveMax)

                  return (
                  <div className="p-2 bg-orange-500/5 border border-orange-500/20 rounded space-y-2">
                    <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="text-xs text-gray-400 block mb-1">Leverage</label>
                      <div className="flex items-center gap-2">
                        <input
                          type="range"
                          min="1"
                          max={effectiveMax}
                          value={currentLev}
                          onChange={e => handleUpdateSettings({ auto_trade_leverage: Number(e.target.value) })}
                          className="flex-1"
                        />
                        <span className="text-xs font-mono text-orange-300 w-10 text-right">
                          {currentLev}x
                        </span>
                      </div>
                      <span className="text-[10px] text-gray-500">
                        Max: {effectiveMax}x{pairMaxLevers.length > 0 ? ' (from pair limits)' : ''}
                      </span>
                    </div>
                    <div>
                      <label className="text-xs text-gray-400 block mb-1">Margin Mode</label>
                      <div className="flex gap-1">
                        {(['crossed', 'isolated'] as const).map(mode => (
                          <button
                            key={mode}
                            onClick={() => handleUpdateSettings({ auto_trade_margin_mode: mode })}
                            className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                              (simAccount.auto_trade_margin_mode || 'crossed') === mode
                                ? mode === 'crossed' ? 'bg-blue-600 text-white' : 'bg-purple-600 text-white'
                                : 'bg-gray-800 text-gray-400 border border-gray-700'
                            }`}
                          >
                            {mode === 'crossed' ? 'Cross' : 'Isolated'}
                          </button>
                        ))}
                      </div>
                    </div>
                    </div>
                    {/* Per-pair leverage limits info */}
                    {selectedPairs.length > 0 && Object.keys(leverageLimits).length > 0 && (
                      <div className="flex flex-wrap gap-2">
                        {selectedPairs.map((p: string) => {
                          const lim = leverageLimits[p]
                          return lim ? (
                            <span key={p} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">
                              {p.split('/')[0]}: max {lim.max}x
                            </span>
                          ) : null
                        })}
                      </div>
                    )}
                  </div>
                  )
                })()}

                {/* Searchable Pair Selector */}
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Auto-Trade Pairs</label>
                  {/* Delisting / adjustment warnings for selected pairs */}
                  {(() => {
                    const warnings = (simAccount.auto_trade_pairs || [])
                      .map((p: string) => availablePairs.find(ap => ap.symbol === p))
                      .filter((ap: AvailablePair | undefined): ap is AvailablePair =>
                        Boolean(
                          ap && (
                            ap.delisting_ts || ap.futures_adjustment || ap.maintain_time || ap.limit_open_time
                            || !['online', 'normal'].includes(ap.status)
                          )
                        )
                      )
                    if (warnings.length === 0) return null
                    return (
                      <div className="mb-2 space-y-1">
                        {warnings.map((w: AvailablePair) => (
                          <div key={w.symbol} className="flex items-start gap-1.5 px-2 py-1 text-[10px] rounded bg-red-500/10 border border-red-500/30 text-red-300">
                            <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                            <span>
                              <strong>{w.symbol}</strong>
                              {w.delisting_date && <> — Delisting: {w.delisting_date}</>}
                              {w.futures_adjustment && <> — Futures: {w.futures_adjustment}</>}
                              {w.maintain_time && <> — Maintenance: {w.maintain_time}</>}
                              {w.limit_open_time && <> — Restricted after: {w.limit_open_time}</>}
                              {!w.delisting_date && !w.futures_adjustment && !w.maintain_time && !w.limit_open_time && <> — Status: {w.status}</>}
                            </span>
                          </div>
                        ))}
                      </div>
                    )
                  })()}
                  {/* Selected pairs as tags */}
                  <div className="flex flex-wrap gap-1.5 mb-2 min-h-[28px]">
                    {(simAccount.auto_trade_pairs || []).map((pair: string) => {
                      const pairInfo = availablePairs.find(ap => ap.symbol === pair)
                      const isWarning = pairInfo && (pairInfo.delisting_ts || pairInfo.futures_adjustment || !['online', 'normal'].includes(pairInfo.status))
                      return (
                        <span
                          key={pair}
                          className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full border ${
                            isWarning
                              ? 'bg-red-600/20 border-red-500/30 text-red-300'
                              : 'bg-blue-600/20 border-blue-500/30 text-blue-300'
                          }`}
                        >
                          {isWarning && <AlertTriangle className="w-2.5 h-2.5" />}
                          {pair}
                          <button
                            onClick={() => {
                              const updated = (simAccount.auto_trade_pairs || []).filter((p: string) => p !== pair)
                              handleUpdateSettings({ auto_trade_pairs: updated })
                            }}
                            className="hover:text-red-400 transition"
                          >
                            <X className="w-3 h-3" />
                          </button>
                        </span>
                      )
                    })}
                    {(simAccount.auto_trade_pairs || []).length === 0 && (
                      <span className="text-xs text-gray-600 italic">No pairs selected — search below to add</span>
                    )}
                  </div>
                  {/* Search input with dropdown */}
                  <div className="relative" ref={pairDropdownRef}>
                    <div className="relative">
                      <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                      <input
                        type="text"
                        value={pairSearch}
                        onChange={e => {
                          setPairSearch(e.target.value)
                          setShowPairDropdown(true)
                        }}
                        onFocus={() => setShowPairDropdown(true)}
                        placeholder={availablePairsLoading ? 'Loading pairs...' : `Search ${availablePairs.length || ''} pairs... (BTC, ETH, SOL)`}
                        className="w-full bg-gray-900 border border-gray-700 rounded pl-8 pr-3 py-1.5 text-xs text-white focus:border-blue-500 outline-none"
                      />
                    </div>
                    {showPairDropdown && (
                      <div className="absolute z-50 w-full mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl max-h-60 overflow-y-auto">
                        {(() => {
                          const q = pairSearch.toUpperCase()
                          const selected = new Set(simAccount.auto_trade_pairs || [])
                          const filtered = (availablePairs.length > 0 ? availablePairs : DEFAULT_PAIRS.map(p => ({ symbol: p, baseCoin: p.split('/')[0], quoteCoin: 'USDT', market: 'spot', status: 'online', delisting_ts: null, delisting_date: null, minLever: null, maxLever: null })))
                            .filter(p => (!q || p.symbol.toUpperCase().includes(q) || p.baseCoin.toUpperCase().includes(q)) && !selected.has(p.symbol))
                            .slice(0, 100) // Limit dropdown to 100 for perf
                          if (filtered.length === 0) {
                            return (
                              <div className="p-3 text-xs text-gray-500 text-center">
                                {pairSearch ? 'No matching pairs' : 'All pairs already added'}
                              </div>
                            )
                          }
                          return filtered.map(pair => {
                            const isDelisting = !!pair.delisting_ts
                            const isAbnormal = pair.status && !['online', 'normal'].includes(pair.status)
                            return (
                              <button
                                key={pair.symbol}
                                onClick={() => {
                                  const updated = [...(simAccount.auto_trade_pairs || []), pair.symbol]
                                  handleUpdateSettings({ auto_trade_pairs: updated })
                                  setPairSearch('')
                                  setShowPairDropdown(false)
                                }}
                                className={`w-full text-left px-3 py-2 text-xs transition flex items-center justify-between gap-2 ${
                                  isDelisting ? 'hover:bg-red-900/30 bg-red-900/10' : 'hover:bg-gray-700'
                                } text-white`}
                              >
                                <div className="flex items-center gap-2 min-w-0">
                                  <span className="font-medium">{pair.symbol}</span>
                                  <span className={`text-[10px] px-1 py-0.5 rounded ${
                                    pair.market === 'both' ? 'bg-purple-500/20 text-purple-300' :
                                    pair.market === 'futures' ? 'bg-orange-500/20 text-orange-300' :
                                    'bg-blue-500/20 text-blue-300'
                                  }`}>
                                    {pair.market === 'both' ? 'S+F' : pair.market === 'futures' ? 'FUT' : 'SPOT'}
                                  </span>
                                  {pair.maxLever && (
                                    <span className="text-[10px] text-gray-500">{pair.maxLever}x</span>
                                  )}
                                </div>
                                <div className="flex items-center gap-1.5 shrink-0">
                                  {isDelisting && (
                                    <span className="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 flex items-center gap-0.5">
                                      <AlertTriangle className="w-2.5 h-2.5" /> Delisting {pair.delisting_date}
                                    </span>
                                  )}
                                  {isAbnormal && !isDelisting && (
                                    <span className="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300">
                                      {pair.status}
                                    </span>
                                  )}
                                  <Plus className="w-3 h-3 text-gray-500" />
                                </div>
                              </button>
                            )
                          })
                        })()}
                      </div>
                    )}
                  </div>
                </div>

                {/* Pine Script / Strategy for Trade Decisions */}
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Pine Script for Entries (50% weight)</label>
                  <select
                    value={simAccount.auto_trade_pine_script_id || ''}
                    onChange={e => handleUpdateSettings({ auto_trade_pine_script_id: Number(e.target.value) || 0 })}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                  >
                    <option value="">None — TA + Sentiment only</option>
                    {pineScripts.map(ps => (
                      <option key={ps.id} value={ps.id}>
                        🌲 {ps.name} {ps.is_active ? '●' : ''} {ps.strategy_id ? '(linked)' : ''}
                      </option>
                    ))}
                  </select>
                  <p className="text-[10px] text-gray-500 mt-1">
                    Selected script is evaluated at the auto-trade timeframe and heavily influences entries
                  </p>
                </div>

                {/* Auto-trade cycle controls */}
                <div className="flex items-center gap-3 pt-1">
                  <button
                    onClick={runAutoTradeCycle}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded bg-blue-600 text-white hover:bg-blue-500 transition"
                  >
                    <Zap className="w-3 h-3" /> Run Single Cycle
                  </button>
                  <button
                    onClick={toggleAutoTradeLoop}
                    className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded transition ${
                      autoTradeRunning
                        ? 'bg-red-600 text-white hover:bg-red-500'
                        : 'bg-green-600 text-white hover:bg-green-500'
                    }`}
                  >
                    {autoTradeRunning ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                    {autoTradeRunning ? 'Stop Auto-Trade' : 'Start Auto-Trade Loop'}
                  </button>
                  {autoTradeRunning && (
                    <span className="text-xs text-green-400 flex items-center gap-1">
                      <Activity className="w-3 h-3 animate-pulse" /> Server loop · every 60s
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ─── Live Dashboard ─── */}
        {isLiveMode && (
          <div className="bg-gradient-to-r from-green-900/20 to-blue-900/20 border border-green-500/30 rounded-lg p-4">
            <div className="flex flex-wrap items-center justify-between gap-4">
              {/* Live Account Stats */}
              <div className="flex flex-wrap gap-6">
                <div>
                  <span className="text-xs text-gray-400 block">Available Balance</span>
                  <span className="text-lg font-bold font-mono text-white">
                    {liveAccount ? `$${liveAccount.balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : '—'}
                  </span>
                  {liveAccount && toZar(liveAccount.balance) && <span className="text-[10px] text-gray-500 block font-mono">{toZar(liveAccount.balance)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Equity</span>
                  <span className="text-lg font-bold font-mono text-white">
                    {liveAccount ? `$${liveAccount.equity.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : '—'}
                  </span>
                  {liveAccount && toZar(liveAccount.equity) && <span className="text-[10px] text-gray-500 block font-mono">{toZar(liveAccount.equity)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Total P&L</span>
                  {(() => {
                    const pnl = liveAccount?.total_pnl ?? 0
                    return (
                      <>
                        <span className={`text-lg font-bold font-mono ${
                          pnl >= 0 ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {pnl >= 0 ? '+' : ''}${pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                        </span>
                        {toZar(pnl) && <span className={`text-[10px] block font-mono ${pnl >= 0 ? 'text-green-700' : 'text-red-700'}`}>{toZar(pnl)}</span>}
                      </>
                    )
                  })()}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Win Rate</span>
                  {(() => {
                    const total = liveAccount?.total_trades ?? 0
                    const wins = liveAccount?.winning_trades ?? 0
                    const rate = total > 0 ? ((wins / total) * 100).toFixed(1) : '0.0'
                    return <span className="text-lg font-bold font-mono text-white">{rate}%</span>
                  })()}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Trades</span>
                  <span className="text-sm font-mono text-white">
                    <span className="text-green-400">{liveAccount?.winning_trades ?? 0}W</span>
                    {' / '}
                    <span className="text-red-400">{liveAccount?.losing_trades ?? 0}L</span>
                    {' / '}
                    {liveAccount?.total_trades ?? 0} total
                  </span>
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Open Positions</span>
                  <span className="text-lg font-bold font-mono text-white">
                    {liveAccount?.open_positions_count ?? 0}
                    <span className="text-sm text-gray-500"> / {liveAccount?.settings?.auto_trade_max_positions ?? liveTradeSettings?.auto_trade_max_positions ?? '?'}</span>
                  </span>
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Unrealized P&L</span>
                  {(() => {
                    const pnl = liveAccount?.unrealized_pnl ?? 0
                    return (
                      <>
                        <span className={`text-lg font-bold font-mono ${
                          pnl >= 0 ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
                        </span>
                        {toZar(pnl) && <span className={`text-[10px] block font-mono ${pnl >= 0 ? 'text-green-700' : 'text-red-700'}`}>{toZar(pnl)}</span>}
                      </>
                    )
                  })()}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">Reserved Margin</span>
                  <span className="text-lg font-bold font-mono text-orange-300">
                    ${(liveAccount?.reserved_margin ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}
                  </span>
                  {toZar(liveAccount?.reserved_margin ?? 0) && <span className="text-[10px] text-orange-700 block font-mono">{toZar(liveAccount?.reserved_margin ?? 0)}</span>}
                </div>
                <div>
                  <span className="text-xs text-gray-400 block">MMR</span>
                  <span className={`text-lg font-bold font-mono ${
                    (liveAccount?.mmr ?? 0) > 80 ? 'text-red-400' : (liveAccount?.mmr ?? 0) > 50 ? 'text-yellow-400' : 'text-green-400'
                  }`}>
                    {(liveAccount?.mmr ?? 0).toFixed(2)}%
                  </span>
                </div>
              </div>

              {/* Active Settings Chips */}
              {liveAccount?.settings && (
                <div className="flex flex-wrap gap-1.5 items-center">
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-blue-500/15 border border-blue-500/30 text-blue-300 font-medium">
                    {liveAccount.settings.auto_trade_timeframe || '1h'}
                  </span>
                  <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
                    liveAccount.settings.auto_trade_mode === 'futures'
                      ? 'bg-orange-500/15 border border-orange-500/30 text-orange-300'
                      : 'bg-blue-500/15 border border-blue-500/30 text-blue-300'
                  }`}>
                    {(liveAccount.settings.auto_trade_mode || 'futures').toUpperCase()}
                    {liveAccount.settings.auto_trade_mode === 'futures' && ` ${liveAccount.settings.auto_trade_leverage}x`}
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-purple-500/15 border border-purple-500/30 text-purple-300 font-medium">
                    Risk: {liveAccount.settings.auto_trade_risk_pct}%
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-500/15 border border-gray-500/30 text-gray-300 font-medium">
                    Pos: {liveAccount?.open_positions_count ?? 0}/{liveAccount.settings.auto_trade_max_positions}
                  </span>
                  {(() => {
                    try {
                      const pairs = JSON.parse(liveAccount.settings.auto_trade_pairs || '[]')
                      return (
                        <span className="text-[10px] px-2 py-0.5 rounded-full bg-gray-500/15 border border-gray-500/30 text-gray-300 font-medium">
                          {pairs.length} pairs
                        </span>
                      )
                    } catch { return null }
                  })()}
                  {liveAccount.settings.auto_trade && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-green-500/15 border border-green-500/30 text-green-300 font-medium animate-pulse">
                      AUTO
                    </span>
                  )}
                  {liveAccount.settings.dry_run && (
                    <span className="text-[10px] px-2 py-0.5 rounded-full bg-yellow-500/15 border border-yellow-500/30 text-yellow-300 font-medium">
                      DRY-RUN
                    </span>
                  )}
                </div>
              )}

              {/* Live Actions */}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowLiveSettings(!showLiveSettings)}
                  className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded bg-blue-600/20 border border-blue-500/30 text-blue-300 hover:bg-blue-600/30 transition"
                >
                  <Settings className="w-3 h-3" /> Settings
                </button>
                <button
                  onClick={refreshLiveData}
                  className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded bg-gray-600/20 border border-gray-500/30 text-gray-300 hover:bg-gray-600/30 transition"
                >
                  <RefreshCw className={`w-3 h-3 ${liveLoading ? 'animate-spin' : ''}`} /> Refresh
                </button>
              </div>
            </div>

            {/* Live auto-trade settings */}
            {showLiveSettings && liveTradeSettings && (
              <div className="mt-3 border-t border-green-500/20 pt-3 space-y-3">
                <h4 className="text-sm font-semibold text-white flex items-center gap-2">
                  <Target className="w-4 h-4 text-blue-400" /> Live Trade Settings
                </h4>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Live Trading</label>
                    <button
                      onClick={() => handleUpdateLiveSettings({ is_active: !liveTradeSettings.is_active })}
                      className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                        liveTradeSettings.is_active
                          ? 'bg-green-600 text-white'
                          : 'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}
                    >
                      {liveTradeSettings.is_active ? 'Active' : 'Inactive'}
                    </button>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Auto-Trade</label>
                    <button
                      onClick={() => handleUpdateLiveSettings({ auto_trade: !liveTradeSettings.auto_trade })}
                      className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                        liveTradeSettings.auto_trade
                          ? 'bg-green-600 text-white'
                          : 'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}
                    >
                      {liveTradeSettings.auto_trade ? 'Enabled' : 'Disabled'}
                    </button>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Trade Mode</label>
                    <div className="flex gap-1">
                      {(['spot', 'futures'] as const).map(mode => (
                        <button
                          key={mode}
                          onClick={() => handleUpdateLiveSettings({ auto_trade_mode: mode })}
                          className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                            (liveTradeSettings.auto_trade_mode || 'futures') === mode
                              ? mode === 'futures' ? 'bg-orange-600 text-white' : 'bg-blue-600 text-white'
                              : 'bg-gray-800 text-gray-400 border border-gray-700'
                          }`}
                        >
                          {mode.charAt(0).toUpperCase() + mode.slice(1)}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
                    <select
                      value={liveTradeSettings.auto_trade_timeframe}
                      onChange={e => handleUpdateLiveSettings({ auto_trade_timeframe: e.target.value })}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    >
                      {timeframes.map(tf => (
                        <option key={tf.value} value={tf.value}>{tf.label}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Dry Run</label>
                    <button
                      onClick={() => handleUpdateLiveSettings({ dry_run: !liveTradeSettings.dry_run })}
                      className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                        liveTradeSettings.dry_run
                          ? 'bg-amber-600 text-white'
                          : 'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}
                    >
                      {liveTradeSettings.dry_run ? 'Planning Only' : 'Real Orders'}
                    </button>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">AI Agents</label>
                    <button
                      onClick={() => handleUpdateLiveSettings({ enable_ai: !liveTradeSettings.enable_ai })}
                      className={`w-full py-1.5 text-xs font-semibold rounded transition ${
                        liveTradeSettings.enable_ai
                          ? 'bg-purple-600 text-white'
                          : 'bg-gray-800 text-gray-400 border border-gray-700'
                      }`}
                    >
                      {liveTradeSettings.enable_ai ? 'AI On' : 'AI Off'}
                    </button>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">
                      Max Positions
                      <span className={`ml-1 font-mono ${
                        (liveAccount?.open_positions_count ?? 0) >= (liveTradeSettings.auto_trade_max_positions ?? 3)
                          ? 'text-red-400' : 'text-emerald-400'
                      }`}>
                        ({liveAccount?.open_positions_count ?? 0}/{liveTradeSettings.auto_trade_max_positions ?? 3})
                      </span>
                    </label>
                    <input
                      type="number"
                      value={liveMaxPositionsInput}
                      min="1"
                      max="100"
                      step="1"
                      onChange={e => { setLiveMaxPositionsInput(e.target.value); setLiveSettingsDirty(true) }}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Risk per Trade %</label>
                    <input
                      type="number"
                      value={liveRiskPctInput}
                      min="0.5"
                      max="10"
                      step="0.5"
                      onChange={e => { setLiveRiskPctInput(e.target.value); setLiveSettingsDirty(true) }}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Order Amount In</label>
                    <div className="flex bg-gray-900 rounded overflow-hidden border border-gray-700">
                      {(['quote', 'base'] as const).map(mode => (
                        <button
                          key={mode}
                          onClick={() => handleUpdateLiveSettings({ auto_trade_amount_mode: mode })}
                          className={`flex-1 px-2 py-1.5 text-xs font-semibold transition ${
                            (liveTradeSettings.auto_trade_amount_mode || 'quote') === mode
                              ? 'bg-blue-600 text-white'
                              : 'text-gray-400 hover:text-gray-200'
                          }`}
                        >
                          {mode === 'quote' ? 'USDT' : 'Pair Qty'}
                        </button>
                      ))}
                    </div>
                    <p className="text-[10px] text-gray-500 mt-1">
                      {(liveTradeSettings.auto_trade_amount_mode || 'quote') === 'quote'
                        ? 'Risk % calculates USDT value, converts to pair quantity'
                        : 'Risk % calculates directly in base pair units'}
                    </p>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Max Position Size</label>
                    <input
                      type="number"
                      value={liveMaxPosSizeInput}
                      min="10"
                      step="10"
                      onChange={e => { setLiveMaxPosSizeInput(e.target.value); setLiveSettingsDirty(true) }}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Max Total Exposure</label>
                    <input
                      type="number"
                      value={liveMaxExposureInput}
                      min="50"
                      step="50"
                      onChange={e => { setLiveMaxExposureInput(e.target.value); setLiveSettingsDirty(true) }}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Margin Size ($)</label>
                    <input
                      type="number"
                      value={liveMarginSizeInput}
                      min="1"
                      step="1"
                      onChange={e => { setLiveMarginSizeInput(e.target.value); setLiveSettingsDirty(true) }}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                      placeholder="10"
                    />
                    <p className="text-[10px] text-gray-500 mt-1">Exact USDT margin per trade</p>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Min DCA Gap %</label>
                    <input
                      type="number"
                      value={liveMinGapInput}
                      min="0.5"
                      step="0.5"
                      onChange={e => { setLiveMinGapInput(e.target.value); setLiveSettingsDirty(true) }}
                      className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                      placeholder="2"
                    />
                    <p className="text-[10px] text-gray-500 mt-1">Min price move % before adding DCA</p>
                  </div>
                </div>

                {liveTradeSettings.dry_run && (
                  <div className="px-3 py-2 rounded border border-amber-500/30 bg-amber-500/10 text-amber-200 text-xs">
                    Dry-run is enabled. Live auto-trade will evaluate signals, size positions, and plan entries/exits without sending orders to Bitget. This mode works even while <code>ENABLE_AUTO_TRADING=false</code>.
                  </div>
                )}

                {/* Futures-specific settings for live */}
                {(liveTradeSettings.auto_trade_mode || 'futures') === 'futures' && (() => {
                  const selectedPairs = liveTradeSettings.auto_trade_pairs || []
                  const pairMaxLevers = selectedPairs
                    .map((p: string) => leverageLimits[p]?.max)
                    .filter((v: number | undefined): v is number => v != null)
                  const effectiveMax = pairMaxLevers.length > 0 ? Math.min(...pairMaxLevers) : 125
                  const currentLev = Math.min(liveTradeSettings.auto_trade_leverage || 10, effectiveMax)
                  return (
                    <div className="p-2 bg-orange-500/5 border border-orange-500/20 rounded space-y-2">
                      <div className="grid grid-cols-2 gap-3">
                        <div>
                          <label className="text-xs text-gray-400 block mb-1">Leverage</label>
                          <div className="flex items-center gap-2">
                            <input
                              type="range"
                              min="1"
                              max={effectiveMax}
                              value={currentLev}
                              onChange={e => handleUpdateLiveSettings({ auto_trade_leverage: Number(e.target.value) })}
                              className="flex-1"
                            />
                            <span className="text-xs font-mono text-orange-300 w-10 text-right">
                              {currentLev}x
                            </span>
                          </div>
                          <span className="text-[10px] text-gray-500">
                            Max: {effectiveMax}x{pairMaxLevers.length > 0 ? ' (from pair limits)' : ''}
                          </span>
                        </div>
                        <div>
                          <label className="text-xs text-gray-400 block mb-1">Margin Mode</label>
                          <div className="flex gap-1">
                            {(['crossed', 'isolated'] as const).map(mode => (
                              <button
                                key={mode}
                                onClick={() => handleUpdateLiveSettings({ auto_trade_margin_mode: mode })}
                                className={`flex-1 py-1.5 text-xs font-semibold rounded transition ${
                                  (liveTradeSettings.auto_trade_margin_mode || 'crossed') === mode
                                    ? mode === 'crossed' ? 'bg-blue-600 text-white' : 'bg-purple-600 text-white'
                                    : 'bg-gray-800 text-gray-400 border border-gray-700'
                                }`}
                              >
                                {mode === 'crossed' ? 'Cross' : 'Isolated'}
                              </button>
                            ))}
                          </div>
                        </div>
                      </div>
                      {/* Per-pair leverage limits info */}
                      {selectedPairs.length > 0 && Object.keys(leverageLimits).length > 0 && (
                        <div className="flex flex-wrap gap-2">
                          {selectedPairs.map((p: string) => {
                            const lim = leverageLimits[p]
                            return lim ? (
                              <span key={p} className="text-[10px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400">
                                {p.split('/')[0]}: max {lim.max}x
                              </span>
                            ) : null
                          })}
                        </div>
                      )}
                    </div>
                  )
                })()}

                {/* Searchable Pair Selector for Live */}
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Auto-Trade Pairs</label>
                  {/* Delisting / adjustment warnings for selected pairs */}
                  {(() => {
                    const warnings = (liveTradeSettings.auto_trade_pairs || [])
                      .map((p: string) => availablePairs.find(ap => ap.symbol === p))
                      .filter((ap: AvailablePair | undefined): ap is AvailablePair =>
                        Boolean(
                          ap && (
                            ap.delisting_ts || ap.futures_adjustment || ap.maintain_time || ap.limit_open_time
                            || !['online', 'normal'].includes(ap.status)
                          )
                        )
                      )
                    if (warnings.length === 0) return null
                    return (
                      <div className="mb-2 space-y-1">
                        {warnings.map((w: AvailablePair) => (
                          <div key={w.symbol} className="flex items-start gap-1.5 px-2 py-1 text-[10px] rounded bg-red-500/10 border border-red-500/30 text-red-300">
                            <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" />
                            <span>
                              <strong>{w.symbol}</strong>
                              {w.delisting_date && <> — Delisting: {w.delisting_date}</>}
                              {w.futures_adjustment && <> — Futures: {w.futures_adjustment}</>}
                              {w.maintain_time && <> — Maintenance: {w.maintain_time}</>}
                              {w.limit_open_time && <> — Restricted after: {w.limit_open_time}</>}
                              {!w.delisting_date && !w.futures_adjustment && !w.maintain_time && !w.limit_open_time && <> — Status: {w.status}</>}
                            </span>
                          </div>
                        ))}
                      </div>
                    )
                  })()}
                  {/* Selected pairs as tags */}
                  <div className="flex flex-wrap gap-1.5 mb-2 min-h-[28px]">
                    {(liveTradeSettings.auto_trade_pairs || []).map((pair: string) => {
                      const pairInfo = availablePairs.find(ap => ap.symbol === pair)
                      const isWarning = pairInfo && (pairInfo.delisting_ts || pairInfo.futures_adjustment || !['online', 'normal'].includes(pairInfo.status))
                      return (
                        <span
                          key={pair}
                          className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full border ${
                            isWarning
                              ? 'bg-red-600/20 border-red-500/30 text-red-300'
                              : 'bg-green-600/20 border-green-500/30 text-green-300'
                          }`}
                        >
                          {isWarning && <AlertTriangle className="w-2.5 h-2.5" />}
                          {pair}
                          <button
                            onClick={() => {
                              const updated = (liveTradeSettings.auto_trade_pairs || []).filter((p: string) => p !== pair)
                              handleUpdateLiveSettings({ auto_trade_pairs: updated })
                            }}
                            className="hover:text-red-400 transition"
                          >
                            <X className="w-3 h-3" />
                          </button>
                        </span>
                      )
                    })}
                    {(liveTradeSettings.auto_trade_pairs || []).length === 0 && (
                      <span className="text-xs text-gray-600 italic">No pairs selected — search below to add</span>
                    )}
                  </div>
                  {/* Search input with dropdown */}
                  <div className="relative" ref={pairDropdownRef}>
                    <div className="relative">
                      <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-500" />
                      <input
                        type="text"
                        value={pairSearch}
                        onChange={e => {
                          setPairSearch(e.target.value)
                          setShowPairDropdown(true)
                        }}
                        onFocus={() => setShowPairDropdown(true)}
                        placeholder={availablePairsLoading ? 'Loading pairs...' : `Search ${availablePairs.length || ''} pairs... (BTC, ETH, SOL)`}
                        className="w-full bg-gray-900 border border-gray-700 rounded pl-8 pr-3 py-1.5 text-xs text-white focus:border-green-500 outline-none"
                      />
                    </div>
                    {showPairDropdown && (
                      <div className="absolute z-50 w-full mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl max-h-60 overflow-y-auto">
                        {(() => {
                          const q = pairSearch.toUpperCase()
                          const selected = new Set(liveTradeSettings.auto_trade_pairs || [])
                          const filtered = (availablePairs.length > 0 ? availablePairs : DEFAULT_PAIRS.map(p => ({ symbol: p, baseCoin: p.split('/')[0], quoteCoin: 'USDT', market: 'spot', status: 'online', delisting_ts: null, delisting_date: null, minLever: null, maxLever: null })))
                            .filter(p => (!q || p.symbol.toUpperCase().includes(q) || p.baseCoin.toUpperCase().includes(q)) && !selected.has(p.symbol))
                            .slice(0, 100)
                          if (filtered.length === 0) {
                            return (
                              <div className="p-3 text-xs text-gray-500 text-center">
                                {pairSearch ? 'No matching pairs' : 'All pairs already added'}
                              </div>
                            )
                          }
                          return filtered.map(pair => {
                            const isDelisting = !!pair.delisting_ts
                            const isAbnormal = pair.status && !['online', 'normal'].includes(pair.status)
                            return (
                              <button
                                key={pair.symbol}
                                onClick={() => {
                                  const updated = [...(liveTradeSettings.auto_trade_pairs || []), pair.symbol]
                                  handleUpdateLiveSettings({ auto_trade_pairs: updated })
                                  setPairSearch('')
                                  setShowPairDropdown(false)
                                }}
                                className={`w-full text-left px-3 py-2 text-xs transition flex items-center justify-between gap-2 ${
                                  isDelisting ? 'hover:bg-red-900/30 bg-red-900/10' : 'hover:bg-gray-700'
                                } text-white`}
                              >
                                <div className="flex items-center gap-2 min-w-0">
                                  <span className="font-medium">{pair.symbol}</span>
                                  <span className={`text-[10px] px-1 py-0.5 rounded ${
                                    pair.market === 'both' ? 'bg-purple-500/20 text-purple-300' :
                                    pair.market === 'futures' ? 'bg-orange-500/20 text-orange-300' :
                                    'bg-blue-500/20 text-blue-300'
                                  }`}>
                                    {pair.market === 'both' ? 'S+F' : pair.market === 'futures' ? 'FUT' : 'SPOT'}
                                  </span>
                                  {pair.maxLever && (
                                    <span className="text-[10px] text-gray-500">{pair.maxLever}x</span>
                                  )}
                                </div>
                                <div className="flex items-center gap-1.5 shrink-0">
                                  {isDelisting && (
                                    <span className="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 flex items-center gap-0.5">
                                      <AlertTriangle className="w-2.5 h-2.5" /> Delisting {pair.delisting_date}
                                    </span>
                                  )}
                                  {isAbnormal && !isDelisting && (
                                    <span className="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300">
                                      {pair.status}
                                    </span>
                                  )}
                                  <Plus className="w-3 h-3 text-gray-500" />
                                </div>
                              </button>
                            )
                          })
                        })()}
                      </div>
                    )}
                  </div>
                </div>

                {/* Pine Script / Strategy for Trade Decisions (Live) */}
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Pine Script for Entries (50% weight)</label>
                  <select
                    value={liveTradeSettings.auto_trade_pine_script_id || ''}
                    onChange={e => handleUpdateLiveSettings({ auto_trade_pine_script_id: Number(e.target.value) || 0 })}
                    className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white"
                  >
                    <option value="">None — TA + Sentiment only</option>
                    {pineScripts.map(ps => (
                      <option key={ps.id} value={ps.id}>
                        🌲 {ps.name} {ps.is_active ? '●' : ''} {ps.strategy_id ? '(linked)' : ''}
                      </option>
                    ))}
                  </select>
                </div>

                {/* Save Settings Button */}
                <div className="flex items-center gap-3 pt-2 border-t border-green-500/20">
                  <button
                    onClick={() => {
                      const batch: any = {}
                      const maxPos = parseInt(liveMaxPositionsInput, 10)
                      if (!isNaN(maxPos) && maxPos >= 1) batch.auto_trade_max_positions = Math.min(maxPos, 100)
                      const riskPct = parseFloat(liveRiskPctInput)
                      if (!isNaN(riskPct) && riskPct >= 0.5) batch.auto_trade_risk_pct = Math.min(riskPct, 10)
                      const posSize = parseFloat(liveMaxPosSizeInput)
                      if (!isNaN(posSize) && posSize >= 10) batch.max_position_size_usdt = posSize
                      const exposure = parseFloat(liveMaxExposureInput)
                      if (!isNaN(exposure) && exposure >= 50) batch.max_total_exposure_usdt = exposure
                      const marginSize = parseFloat(liveMarginSizeInput)
                      if (!isNaN(marginSize) && marginSize >= 1) batch.margin_size_usdt = marginSize
                      const minGap = parseFloat(liveMinGapInput)
                      if (!isNaN(minGap) && minGap >= 0.5) batch.min_entry_gap_pct = Math.min(minGap, 20)
                      if (Object.keys(batch).length > 0) {
                        handleUpdateLiveSettings(batch)
                        setLiveSettingsDirty(false)
                      }
                    }}
                    disabled={!liveSettingsDirty}
                    className={`flex items-center gap-1.5 px-4 py-2 text-xs font-semibold rounded transition ${
                      liveSettingsDirty
                        ? 'bg-blue-600 text-white hover:bg-blue-500 shadow-lg shadow-blue-600/20'
                        : 'bg-gray-800 text-gray-500 border border-gray-700 cursor-not-allowed'
                    }`}
                  >
                    <Settings className="w-3.5 h-3.5" />
                    {liveSettingsDirty ? 'Save Settings' : 'Settings Saved'}
                  </button>
                  {liveSettingsDirty && (
                    <span className="text-xs text-amber-400 flex items-center gap-1">
                      <AlertTriangle className="w-3 h-3" /> Unsaved changes
                    </span>
                  )}
                </div>

                {/* Auto-trade cycle controls for Live */}
                <div className="flex items-center gap-3 pt-1">
                  <button
                    onClick={runLiveAutoTradeCycle}
                    className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded transition ${
                      liveTradeSettings.dry_run
                        ? 'bg-amber-600 text-white hover:bg-amber-500'
                        : 'bg-green-600 text-white hover:bg-green-500'
                    }`}
                  >
                    <Zap className="w-3 h-3" /> {liveTradeSettings.dry_run ? 'Run Dry-Run Cycle' : 'Run Single Cycle'}
                  </button>
                  <button
                    onClick={toggleLiveAutoTradeLoop}
                    className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded transition ${
                      liveAutoTradeRunning
                        ? 'bg-red-600 text-white hover:bg-red-500'
                        : 'bg-green-600 text-white hover:bg-green-500'
                    }`}
                  >
                    {liveAutoTradeRunning ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                    {liveAutoTradeRunning
                      ? `Stop ${liveTradeSettings.dry_run ? 'Dry-Run' : 'Live'} Auto-Trade`
                      : `Start ${liveTradeSettings.dry_run ? 'Dry-Run' : 'Live'} Auto-Trade Loop`}
                  </button>
                  {liveAutoTradeRunning && (
                    <span className="text-xs text-green-400 flex items-center gap-1">
                      <Activity className="w-3 h-3 animate-pulse" /> Server loop · every 60s
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ─── Controls Bar ─── */}
        <div className="flex flex-wrap gap-3 items-end">
          {/* Searchable Pair selector */}
          <div className="relative" ref={chartPairRef}>
            <label className="text-xs text-gray-400 block mb-1">Trading Pair</label>
            <input
              type="text"
              value={chartPairOpen ? chartPairQuery : selectedSymbol}
              onChange={(e) => { setChartPairQuery(e.target.value.toUpperCase()); setChartPairOpen(true) }}
              onFocus={() => { setChartPairQuery(''); setChartPairOpen(true) }}
              placeholder="Search pair…"
              className="bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:border-blue-500 outline-none text-sm min-w-[160px] w-[160px]"
            />
            {chartPairOpen && (
              <ul className="absolute z-50 mt-1 max-h-60 w-full overflow-auto rounded border border-gray-600 bg-gray-800 shadow-lg">
                {configuredPairs
                  .filter(p => !chartPairQuery || p.toUpperCase().includes(chartPairQuery))
                  .map(pair => (
                    <li
                      key={pair}
                      onMouseDown={(e) => { e.preventDefault(); setSelectedSymbol(pair); setChartPairOpen(false); setChartPairQuery('') }}
                      className={`px-3 py-2 text-sm cursor-pointer hover:bg-gray-700 ${
                        pair === selectedSymbol ? 'bg-blue-600/30 text-blue-300' : 'text-white'
                      }`}
                    >
                      {pair}
                    </li>
                  ))}
                {configuredPairs.filter(p => !chartPairQuery || p.toUpperCase().includes(chartPairQuery)).length === 0 && (
                  <li className="px-3 py-2 text-sm text-gray-500 italic">No matches</li>
                )}
              </ul>
            )}
          </div>

          {/* Exchange */}
          <div>
            <label className="text-xs text-gray-400 block mb-1">Exchange</label>
            <select
              value={selectedExchange}
              onChange={(e) => setSelectedExchange(e.target.value)}
              className="bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:border-blue-500 outline-none text-sm"
            >
              <option value="bitget">Bitget</option>
              <option value="binance">Binance</option>
              <option value="bybit">Bybit</option>
              <option value="okx">OKX</option>
              <option value="kucoin">KuCoin</option>
              <option value="coinbase">Coinbase</option>
            </select>
          </div>

          {/* Timeframe */}
          <div>
            <label className="text-xs text-gray-400 block mb-1">Timeframe</label>
            <div className="flex gap-1">
              {timeframes.map((tf) => (
                <button
                  key={tf.value}
                  onClick={() => setSelectedTimeframe(tf.value)}
                  className={`px-3 py-2 rounded text-sm transition ${
                    selectedTimeframe === tf.value
                      ? 'bg-blue-600 text-white font-semibold'
                      : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                  }`}
                >
                  {tf.label}
                </button>
              ))}
            </div>
          </div>

          {/* Ticker mini */}
          {ticker && (
            <div className="ml-auto flex items-center gap-4 text-sm">
              <div>
                <span className="text-gray-500 text-xs">Last</span>
                <div className="font-mono font-bold text-white">
                  {formatPrice(ticker.last)}
                </div>
              </div>
              <div className={ticker.percentage >= 0 ? 'text-green-400' : 'text-red-400'}>
                <span className="text-xs">{ticker.percentage >= 0 ? '+' : ''}{ticker.percentage?.toFixed(2)}%</span>
              </div>
              <div className="text-xs text-gray-500">
                H: {formatPrice(ticker.high)} / L: {formatPrice(ticker.low)}
              </div>
            </div>
          )}
        </div>

        {/* ─── Main Grid: Chart + Order Panel ─── */}
        <div className={chartMaximized
          ? 'fixed inset-0 z-50 bg-gray-900 p-4 overflow-auto'
          : 'grid grid-cols-1 lg:grid-cols-4 gap-5'
        }>
          {/* Chart */}
          <div className={chartMaximized
            ? 'bg-gray-800/30 border border-gray-700 rounded-lg p-4'
            : 'lg:col-span-3 bg-gray-800/30 border border-gray-700 rounded-lg p-4'
          }>
            {/* Chart Mode Toggle */}
            <div className="mb-3 flex items-center gap-3 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="text-xs text-gray-400 font-medium">Chart:</span>
                <div className="flex bg-gray-900/60 rounded-md p-0.5">
                  <button
                    onClick={() => setChartMode('tradingview')}
                    className={`px-3 py-1 rounded text-xs font-medium transition ${
                      chartMode === 'tradingview'
                        ? 'bg-blue-600 text-white'
                        : 'text-gray-400 hover:text-white'
                    }`}
                  >
                    TradingView
                  </button>
                  <button
                    onClick={() => setChartMode('custom')}
                    className={`px-3 py-1 rounded text-xs font-medium transition ${
                      chartMode === 'custom'
                        ? 'bg-blue-600 text-white'
                        : 'text-gray-400 hover:text-white'
                    }`}
                  >
                    Custom
                  </button>
                </div>
              </div>

              {/* TradingView Studies Manager */}
              {chartMode === 'tradingview' && (
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs text-gray-500">Indicators:</span>
                  {tvStudies.map((study, i) => (
                    <span key={i} className="flex items-center gap-1 bg-gray-700/50 text-xs text-cyan-300 px-2 py-0.5 rounded">
                      {formatTradingViewStudyLabel(study.id)}
                      <button
                        onClick={() => setTvStudies(prev => prev.filter((_, idx) => idx !== i))}
                        className="text-gray-500 hover:text-red-400 ml-0.5"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </span>
                  ))}
                  <button
                    onClick={() => {
                      const input = prompt(
                        'Enter TradingView study ID:\n\n' +
                        'Examples:\n' +
                        `  ${SMART_MONEY_CONCEPTS_STUDY_ID} (Smart Money Concepts)\n` +
                        '  STD;RSI\n' +
                        '  STD;MACD\n' +
                        '  STD;Bollinger%1Bands\n' +
                        '  STD;Volume%1Profile\n' +
                        '  PUB;YOUR_SCRIPT_ID\n\n' +
                        'Use STD; for built-in, PUB; for community scripts.\n' +
                        'Replace spaces with %1'
                      );
                      if (input?.trim()) {
                        const normalized = normalizeTradingViewStudyId(input);
                        if (!normalized) {
                          window.alert('Invalid study ID. Use STD;... or PUB;... format.');
                          return;
                        }

                        setTvStudies(prev =>
                          normalizeTradingViewStudies([...prev, { id: normalized }])
                        );
                      }
                    }}
                    className="text-xs text-gray-500 hover:text-cyan-400 px-2 py-0.5 rounded border border-dashed border-gray-600 hover:border-cyan-500 transition"
                  >
                    + Add
                  </button>
                </div>
              )}
            </div>

            {/* Strategy / Pine Script Multi-Selector — only shown in custom chart mode */}
            {chartMode === 'custom' && (
            <div className="mb-3 flex items-center gap-3 flex-wrap">
              <div className="flex items-center gap-2">
                <Activity className="w-4 h-4 text-cyan-400" />
                <span className="text-xs text-gray-400 font-medium">Chart Overlays:</span>
              </div>
              <div className="relative" ref={overlayDropdownRef}>
                <button
                  onClick={() => setShowOverlayDropdown(!showOverlayDropdown)}
                  className="bg-gray-900/50 border border-gray-600 rounded px-3 py-1.5 text-xs text-white focus:border-cyan-500 focus:outline-none min-w-[200px] text-left flex items-center justify-between gap-2"
                >
                  <span className="truncate">
                    {overlaySelections.length === 0
                      ? 'Select indicators...'
                      : `${overlaySelections.length} selected`}
                  </span>
                  <svg className={`w-3 h-3 transition-transform ${showOverlayDropdown ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {showOverlayDropdown && (
                  <div className="absolute top-full left-0 mt-1 w-72 bg-gray-900 border border-gray-600 rounded-lg shadow-xl z-50 max-h-64 overflow-y-auto">
                    {/* Clear all */}
                    {overlaySelections.length > 0 && (
                      <button
                        onClick={() => {
                          setOverlaySelections([])
                          setStrategyOverlays([])
                          setStrategyMarkers([])
                          setStrategyEval(null)
                        }}
                        className="w-full text-left px-3 py-1.5 text-xs text-red-400 hover:bg-red-500/10 border-b border-gray-700"
                      >
                        Clear all selections
                      </button>
                    )}
                    {/* Strategies */}
                    {strategies.length > 0 && (
                      <>
                        <div className="px-3 py-1 text-[10px] font-semibold text-gray-500 uppercase tracking-wider bg-gray-800/50">
                          Strategies
                        </div>
                        {strategies.map(s => {
                          const val = `strategy:${s.id}`
                          const checked = overlaySelections.includes(val)
                          return (
                            <label
                              key={val}
                              className={`flex items-center gap-2 px-3 py-1.5 text-xs cursor-pointer hover:bg-gray-800 transition ${
                                checked ? 'text-cyan-300 bg-cyan-500/10' : 'text-gray-300'
                              }`}
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleOverlaySelection(val)}
                                className="w-3 h-3 rounded border-gray-500 text-cyan-500 focus:ring-0 bg-gray-800"
                              />
                              <span className="truncate">{s.name}</span>
                              {s.is_active && <span className="text-green-400 text-[10px]">●</span>}
                            </label>
                          )
                        })}
                      </>
                    )}
                    {/* Pine Scripts */}
                    {pineScripts.length > 0 && (
                      <>
                        <div className="px-3 py-1 text-[10px] font-semibold text-gray-500 uppercase tracking-wider bg-gray-800/50">
                          Pine Scripts
                        </div>
                        {pineScripts.map(ps => {
                          const val = `pine:${ps.id}`
                          const checked = overlaySelections.includes(val)
                          return (
                            <label
                              key={val}
                              className={`flex items-center gap-2 px-3 py-1.5 text-xs cursor-pointer hover:bg-gray-800 transition ${
                                checked ? 'text-cyan-300 bg-cyan-500/10' : 'text-gray-300'
                              }`}
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleOverlaySelection(val)}
                                className="w-3 h-3 rounded border-gray-500 text-cyan-500 focus:ring-0 bg-gray-800"
                              />
                              <span className="truncate">{ps.name}</span>
                              {ps.is_active && <span className="text-green-400 text-[10px]">●</span>}
                              {ps.strategy_id && <span className="text-gray-500 text-[10px]">(linked)</span>}
                            </label>
                          )
                        })}
                      </>
                    )}
                    {strategies.length === 0 && pineScripts.length === 0 && (
                      <div className="px-3 py-3 text-xs text-gray-500 text-center">
                        No strategies or pine scripts created yet.
                        <br />Go to Strategies page to create one.
                      </div>
                    )}
                  </div>
                )}
              </div>
              {/* Selected chips */}
              {overlaySelections.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {overlaySelections.map(sel => {
                    const isStrategy = sel.startsWith('strategy:')
                    const id = Number(sel.split(':')[1])
                    const name = isStrategy
                      ? strategies.find(s => s.id === id)?.name || `Strategy #${id}`
                      : pineScripts.find(ps => ps.id === id)?.name || `Pine #${id}`
                    return (
                      <span
                        key={sel}
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium ${
                          isStrategy
                            ? 'bg-blue-500/15 border border-blue-500/30 text-blue-300'
                            : 'bg-emerald-500/15 border border-emerald-500/30 text-emerald-300'
                        }`}
                      >
                        {name}
                        <button
                          onClick={() => toggleOverlaySelection(sel)}
                          className="ml-0.5 hover:text-white"
                        >
                          <X className="w-2.5 h-2.5" />
                        </button>
                      </span>
                    )
                  })}
                </div>
              )}
              {strategyLoading && (
                <RefreshCw className="w-3 h-3 text-cyan-400 animate-spin" />
              )}
              {strategyEval && (
                <div className="flex items-center gap-2 text-xs">
                  <span className={`font-bold px-1.5 py-0.5 rounded ${
                    strategyEval.action === 'buy' ? 'bg-green-500/20 text-green-400' :
                    strategyEval.action === 'sell' ? 'bg-red-500/20 text-red-400' :
                    'bg-gray-600/30 text-gray-400'
                  }`}>
                    {strategyEval.action.toUpperCase()}
                  </span>
                  <span className={`font-mono ${
                    strategyEval.score > 0 ? 'text-green-400' : strategyEval.score < 0 ? 'text-red-400' : 'text-gray-400'
                  }`}>
                    {strategyEval.score > 0 ? '+' : ''}{strategyEval.score.toFixed(4)}
                  </span>
                  {/* Show individual eval results for multi-select */}
                  {strategyEval.eval_results && strategyEval.eval_results.length > 1 && (
                    <div className="flex gap-1.5">
                      {strategyEval.eval_results.map((er, i) => (
                        <span
                          key={i}
                          className={`text-[10px] px-1 py-0.5 rounded ${
                            er.action === 'buy' ? 'bg-green-500/10 text-green-400' :
                            er.action === 'sell' ? 'bg-red-500/10 text-red-400' :
                            'bg-gray-600/10 text-gray-500'
                          }`}
                          title={`${er.name}: ${er.score > 0 ? '+' : ''}${er.score.toFixed(3)}`}
                        >
                          {er.name.slice(0, 12)}: {er.action[0].toUpperCase()}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
            )}

            {/* Chart — TradingView Widget or Custom Lightweight Charts */}
            {chartMode === 'tradingview' ? (
              <TradingViewWidget
                symbol={selectedSymbol}
                exchange={selectedExchange}
                timeframe={selectedTimeframe}
                studies={tvStudies}
                maximized={chartMaximized}
                onToggleMaximize={() => setChartMaximized(m => !m)}
              />
            ) : (
            <TradingViewChart
              symbol={selectedSymbol}
              exchange={selectedExchange}
              timeframe={selectedTimeframe}
              overlays={strategyOverlays}
              markers={strategyMarkers}
              strategyName={strategyEval?.name}
              strategyScore={strategyEval?.score}
              strategyAction={strategyEval?.action}
              maximized={chartMaximized}
              onToggleMaximize={() => setChartMaximized(m => !m)}
              onSlTpChange={isLiveMode ? handleChartSlTpChange : undefined}
              simPositions={isSimMode && simActive ? simPositions.map(p => ({
                symbol: p.symbol,
                side: p.side,
                entry_price: p.entry_price,
                stop_loss: p.stop_loss,
                take_profit: p.take_profit,
                amount: p.amount,
                unrealized_pnl: p.unrealized_pnl,
              })) : isLiveMode && liveAccount ? liveAccount.open_positions.map(p => ({
                symbol: p.symbol.replace(/USDT$/, '/USDT').replace(/USDC$/, '/USDC'),
                side: p.side,
                entry_price: p.entry_price,
                stop_loss: p.stop_loss ?? null,
                take_profit: p.take_profit ?? null,
                amount: p.amount,
                unrealized_pnl: p.unrealized_pnl,
              })) : []}
              limitOrders={isLiveMode ? liveOpenOrders.filter(o => (o.orderType || 'limit').toLowerCase() === 'limit').map(o => {
                const chartSymbol = o.symbol
                  .replace(/_UMCBL$|_DMCBL$|_CMCBL$/, '')
                  .replace(/USDT$/, '/USDT')
                  .replace(/USDC$/, '/USDC');
                return {
                  orderId: o.orderId,
                  symbol: chartSymbol,
                  side: o.side || '',
                  price: Number(o.price) || 0,
                  size: o.size || '0',
                  orderType: o.orderType || 'limit',
                  stopLoss: Number(o.stopLoss || o.presetStopLossPrice || 0) || undefined,
                  takeProfit: Number(o.takeProfit || o.presetStopSurplusPrice || 0) || undefined,
                };
              }) : []}
            />
            )}
          </div>

          {/* Order Panel — hidden when chart is maximized */}
          <div className={`space-y-4 ${chartMaximized ? 'hidden' : ''}`}>
            {/* Manual Order Form */}
            <div className={`bg-gray-800/30 border rounded-lg p-4 space-y-3 ${
              isSimMode && simActive ? 'border-purple-500/40' : isLiveMode ? 'border-green-500/40' : 'border-gray-700'
            }`}>
              <h3 className="font-semibold text-white text-sm flex items-center gap-2">
                <DollarSign className="w-4 h-4 text-blue-400" /> Place Order
                {isSimMode && simActive && (
                  <span className="text-[10px] font-medium bg-purple-500/30 text-purple-300 px-1.5 py-0.5 rounded">
                    SIM
                  </span>
                )}
              </h3>

              {/* Spot / Futures tab */}
              <div className="grid grid-cols-2 gap-1 bg-gray-900 rounded p-0.5">
                <button
                  onClick={() => setOrderTradeType('spot')}
                  className={`py-1.5 rounded text-xs font-semibold transition ${
                    orderTradeType === 'spot'
                      ? 'bg-blue-600 text-white'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  Spot
                </button>
                <button
                  onClick={() => setOrderTradeType('futures')}
                  className={`py-1.5 rounded text-xs font-semibold transition ${
                    orderTradeType === 'futures'
                      ? 'bg-orange-600 text-white'
                      : 'text-gray-400 hover:text-gray-200'
                  }`}
                >
                  Futures
                </button>
              </div>

              {/* Buy / Sell toggle */}
              <div className="grid grid-cols-2 gap-2">
                <button
                  onClick={() => setOrderSide('buy')}
                  className={`py-2 rounded font-semibold text-sm transition ${
                    orderSide === 'buy'
                      ? 'bg-green-600 text-white'
                      : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                  }`}
                >
                  <TrendingUp className="w-4 h-4 inline mr-1" /> Buy
                </button>
                <button
                  onClick={() => setOrderSide('sell')}
                  className={`py-2 rounded font-semibold text-sm transition ${
                    orderSide === 'sell'
                      ? 'bg-red-600 text-white'
                      : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
                  }`}
                >
                  <TrendingDown className="w-4 h-4 inline mr-1" /> Sell
                </button>
              </div>

              {/* Order type */}
              <div className="flex gap-2">
                {(['market', 'limit'] as const).map(t => (
                  <button
                    key={t}
                    onClick={() => setOrderType(t)}
                    className={`flex-1 py-1.5 rounded text-xs font-medium transition ${
                      orderType === t
                        ? 'bg-blue-600/20 border border-blue-500 text-blue-300'
                        : 'bg-gray-800 border border-gray-700 text-gray-400'
                    }`}
                  >
                    {t.charAt(0).toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>

              {/* Price (limit only) */}
              {orderType === 'limit' && (
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Price (USDT)</label>
                  <input
                    type="number"
                    value={orderPrice}
                    onChange={e => setOrderPrice(e.target.value)}
                    placeholder={ticker ? ticker.last.toString() : '0.00'}
                    step="any"
                    className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                  />
                </div>
              )}

              {/* Futures: Margin mode & Leverage */}
              {orderTradeType === 'futures' && (
                <div className="space-y-2 p-2 bg-orange-500/5 border border-orange-500/20 rounded">
                  {/* Margin Mode */}
                  <div>
                    <label className="text-xs text-gray-400 block mb-1">Margin Mode</label>
                    <div className="grid grid-cols-2 gap-1">
                      {(['crossed', 'isolated'] as const).map(m => (
                        <button
                          key={m}
                          onClick={() => setOrderMarginMode(m)}
                          className={`py-1 rounded text-xs font-medium transition ${
                            orderMarginMode === m
                              ? 'bg-orange-600/30 border border-orange-500 text-orange-300'
                              : 'bg-gray-800 border border-gray-700 text-gray-400'
                          }`}
                        >
                          {m.charAt(0).toUpperCase() + m.slice(1)}
                        </button>
                      ))}
                    </div>
                  </div>
                  {/* Leverage */}
                  <div>
                    <label className="text-xs text-gray-400 flex justify-between mb-1">
                      <span>Leverage</span>
                      <span className="text-orange-300 font-mono">{orderLeverage}x</span>
                    </label>
                    <input
                      type="range"
                      min={leverageLimits[selectedSymbol]?.min || 1}
                      max={leverageLimits[selectedSymbol]?.max || 125}
                      value={orderLeverage}
                      onChange={e => setOrderLeverage(Number(e.target.value))}
                      className="w-full h-1.5 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-orange-500"
                    />
                    <div className="flex justify-between text-[10px] text-gray-500 mt-0.5">
                      <span>{leverageLimits[selectedSymbol]?.min || 1}x</span>
                      <span>{leverageLimits[selectedSymbol]?.max || 125}x</span>
                    </div>
                  </div>
                </div>
              )}

              {/* Amount */}
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs text-gray-400">
                    Amount ({orderAmountMode === 'quote' ? 'USDT' : selectedSymbol.split('/')[0]})
                  </label>
                  <div className="flex bg-gray-900 rounded overflow-hidden border border-gray-700">
                    <button
                      onClick={() => { setOrderAmountMode('base'); setOrderAmount('') }}
                      className={`px-2 py-0.5 text-[10px] font-semibold transition ${
                        orderAmountMode === 'base'
                          ? 'bg-blue-600 text-white'
                          : 'text-gray-400 hover:text-white'
                      }`}
                    >
                      {selectedSymbol.split('/')[0]}
                    </button>
                    <button
                      onClick={() => { setOrderAmountMode('quote'); setOrderAmount('') }}
                      className={`px-2 py-0.5 text-[10px] font-semibold transition ${
                        orderAmountMode === 'quote'
                          ? 'bg-blue-600 text-white'
                          : 'text-gray-400 hover:text-white'
                      }`}
                    >
                      USDT
                    </button>
                  </div>
                </div>
                <input
                  type="number"
                  value={orderAmount}
                  onChange={e => setOrderAmount(e.target.value)}
                  placeholder={orderAmountMode === 'quote' ? '100.00' : '0.00'}
                  step="any"
                  className="w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:border-blue-500 outline-none"
                />
              </div>

              {/* Estimated conversion */}
              <div className="flex justify-between text-xs text-gray-400">
                {orderAmountMode === 'quote' ? (
                  <>
                    <span>≈ Qty</span>
                    <span className="font-mono">{estimatedTotal()} {selectedSymbol.split('/')[0]}</span>
                  </>
                ) : (
                  <>
                    <span>Est. Total</span>
                    <span className="font-mono">${estimatedTotal()} USDT</span>
                  </>
                )}
              </div>

              {/* TP/SL Settings */}
              {orderTradeType === 'futures' && (
                <div className="space-y-2 p-2 bg-blue-500/5 border border-blue-500/20 rounded">
                  <div className="flex items-center justify-between">
                    <label className="text-xs text-gray-400">Auto TP/SL</label>
                    <button
                      onClick={() => setOrderAutoSlTp(!orderAutoSlTp)}
                      className={`w-8 h-4 rounded-full transition-colors relative ${
                        orderAutoSlTp ? 'bg-blue-600' : 'bg-gray-600'
                      }`}
                    >
                      <div className={`w-3 h-3 bg-white rounded-full absolute top-0.5 transition-all ${
                        orderAutoSlTp ? 'left-4' : 'left-0.5'
                      }`} />
                    </button>
                  </div>
                  {orderAutoSlTp && (
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="text-[10px] text-red-400 block mb-0.5">Stop Loss %</label>
                        <input
                          type="number"
                          value={orderSlPct}
                          onChange={e => setOrderSlPct(e.target.value)}
                          placeholder="2"
                          step="0.5"
                          min="0.5"
                          max="50"
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:border-red-500 outline-none"
                        />
                      </div>
                      <div>
                        <label className="text-[10px] text-green-400 block mb-0.5">Take Profit %</label>
                        <input
                          type="number"
                          value={orderTpPct}
                          onChange={e => setOrderTpPct(e.target.value)}
                          placeholder="4"
                          step="0.5"
                          min="0.5"
                          max="100"
                          className="w-full bg-gray-900 border border-gray-700 rounded px-2 py-1.5 text-xs text-white focus:border-green-500 outline-none"
                        />
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Place order button */}
              <button
                onClick={placeOrder}
                disabled={orderLoading || !orderAmount || Number(orderAmount) <= 0}
                className={`w-full py-2.5 rounded font-semibold text-sm transition ${
                  orderSide === 'buy'
                    ? 'bg-green-600 hover:bg-green-500 disabled:bg-green-900/30 text-white disabled:text-green-800'
                    : 'bg-red-600 hover:bg-red-500 disabled:bg-red-900/30 text-white disabled:text-red-800'
                }`}
              >
                {orderLoading ? (
                  <RefreshCw className="w-4 h-4 animate-spin inline mr-1" />
                ) : null}
                {simActive ? '[SIM] ' : isLiveMode ? '[LIVE] ' : ''}{orderSide === 'buy' ? 'Buy' : 'Sell'} {selectedSymbol.split('/')[0]}
              </button>

              {/* Balance info */}
              {isSimMode && simActive ? (
                <div className="text-xs text-purple-400 pt-1 border-t border-purple-500/20">
                  Sim Equity: ${simAccount?.equity.toLocaleString(undefined, { minimumFractionDigits: 2 })} · Available: ${simAccount?.balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </div>
              ) : isLiveMode && liveAccount ? (
                <div className="text-xs text-green-400 pt-1 border-t border-green-500/20">
                  Live Equity: ${liveAccount.equity.toLocaleString(undefined, { minimumFractionDigits: 2 })} · Available: ${liveAccount.balance.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </div>
              ) : balance ? (
                <div className="text-xs text-gray-500 pt-1 border-t border-gray-700/50">
                  Available: {JSON.stringify(balance.free || balance.balance || balance).substring(0, 60)}
                </div>
              ) : null}
            </div>

            {/* Order result toast */}
            {orderResult && (
              <div className={`border rounded-lg p-3 text-sm ${
                orderResult.success
                  ? 'bg-green-500/10 border-green-500/30 text-green-300'
                  : 'bg-red-500/10 border-red-500/30 text-red-300'
              }`}>
                <div className="flex items-start gap-2">
                  {orderResult.success
                    ? <CheckCircle className="w-4 h-4 shrink-0 mt-0.5" />
                    : <XCircle className="w-4 h-4 shrink-0 mt-0.5" />
                  }
                  <span>{orderResult.message}</span>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ─── Sim Positions & Orders ─── */}
        {isSimMode && simActive && (
          <div className="bg-gray-800/30 border border-purple-500/30 rounded-lg p-5">
            {/* Tab toggle */}
            <div className="flex items-center gap-4 mb-4">
              <button
                onClick={() => setSimTab('positions')}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  simTab === 'positions'
                    ? 'border-purple-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Open Positions ({simPositions.length})
              </button>
              <button
                onClick={() => { setSimTab('closed'); fetchClosedPositions() }}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  simTab === 'closed'
                    ? 'border-purple-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Closed ({closedPositions.length})
              </button>
              <button
                onClick={() => setSimTab('orders')}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  simTab === 'orders'
                    ? 'border-purple-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Order History ({simOrders.length})
              </button>
              <button
                onClick={refreshSimData}
                className="ml-auto text-gray-400 hover:text-white"
                title="Refresh"
              >
                <RefreshCw className="w-4 h-4" />
              </button>
              {simTab === 'positions' && simPositions.length > 0 && (
                <>
                  {simPositions.some(p => !p.stop_loss || !p.take_profit) && (
                    <button
                      onClick={async () => {
                        try {
                          await apiClient.backfillSimSlTp()
                          await refreshSimData()
                        } catch {}
                      }}
                      disabled={simLoading}
                      className="text-xs px-3 py-1 rounded bg-yellow-600/80 hover:bg-yellow-500 text-white font-medium disabled:opacity-50 flex items-center gap-1"
                      title="Analyse and add SL/TP for positions missing them"
                    >
                      <Shield className="w-3 h-3" /> Add SL/TP
                    </button>
                  )}
                  <button
                    onClick={closeAllSimPositions}
                    disabled={simLoading}
                    className="text-xs px-3 py-1 rounded bg-red-600/80 hover:bg-red-500 text-white font-medium disabled:opacity-50"
                  >
                    Close All
                  </button>
                </>
              )}
            </div>

            {simTab === 'positions' ? (
              simPositions.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-6">No open positions</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-700">
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-left py-2 px-2">Type</th>
                        <th className="text-left py-2 px-2">Side</th>
                        <th className="text-right py-2 px-2">Amount</th>
                        <th className="text-right py-2 px-2">Cost</th>
                        <th className="text-right py-2 px-2">Value</th>
                        <th className="text-right py-2 px-2">Entry</th>
                        <th className="text-right py-2 px-2">Current</th>
                        <th className="text-center py-2 px-2">Margin</th>
                        <th className="text-center py-2 px-2">Lev</th>
                        <th className="text-right py-2 px-2">SL</th>
                        <th className="text-right py-2 px-2">TP</th>
                        <th className="text-right py-2 px-2">PnL</th>
                        <th className="text-center py-2 px-2">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {simPositions.map(pos => {
                        const isFutures = (pos.trade_type || 'spot') === 'futures'
                        // Use ROE% from backend if available (leverage-adjusted), else compute
                        const roePct = pos.unrealized_roe_pct != null
                          ? pos.unrealized_roe_pct
                          : pos.entry_price > 0
                            ? ((pos.unrealized_pnl / (pos.entry_price * pos.amount)) * 100 * (isFutures ? (pos.leverage || 1) : 1))
                            : 0
                        const costUsd = pos.margin != null ? pos.margin : pos.amount * pos.entry_price
                        const valueUsd = pos.amount * pos.current_price
                        return (
                          <tr key={pos.id} className="border-b border-gray-800 hover:bg-gray-800/40 cursor-pointer" onClick={() => setSelectedSymbol(pos.symbol)}>
                            <td className="py-2 px-2 font-medium text-white underline decoration-dotted underline-offset-2">{pos.symbol}</td>
                            <td className="py-2 px-2">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                isFutures
                                  ? 'bg-orange-500/20 text-orange-400'
                                  : 'bg-blue-500/20 text-blue-400'
                              }`}>
                                {isFutures ? 'Futures' : 'Spot'}
                              </span>
                            </td>
                            <td className="py-2 px-2">
                              <span className={`font-semibold ${pos.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                                {pos.side.toUpperCase()}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{pos.amount.toFixed(6)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-400">
                              ${costUsd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                              {isFutures && <span className="text-[10px] text-gray-500 ml-1">margin</span>}
                            </td>
                            <td className={`py-2 px-2 text-right font-mono font-medium ${valueUsd >= costUsd ? 'text-green-400' : 'text-red-400'}`}>${valueUsd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(pos.entry_price)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(pos.current_price)}</td>
                            <td className="py-2 px-2 text-center">
                              {isFutures ? (
                                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                  pos.margin_mode === 'isolated'
                                    ? 'bg-purple-500/20 text-purple-400'
                                    : 'bg-blue-500/20 text-blue-400'
                                }`}>
                                  {pos.margin_mode === 'isolated' ? 'Isolated' : 'Cross'}
                                </span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-center">
                              {isFutures && pos.leverage ? (
                                <span className="text-orange-300 font-mono font-semibold">{pos.leverage}x</span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-red-400">
                              {pos.stop_loss ? (
                                <span
                                  title={pos.sl_type === 'trailing' || pos.sl_type === 'signal'
                                    ? `Trailing SL: ${formatPrice(pos.stop_loss)}`
                                    : `Stop Loss: ${formatPrice(pos.stop_loss)}`}
                                  className={pos.sl_type === 'trailing' ? 'text-emerald-400' : 'text-red-400'}
                                >
                                  {pos.sl_type === 'trailing' ? '🔒 ' : ''}{formatPrice(pos.stop_loss)}
                                </span>
                              ) : <span className="text-yellow-500" title="No Stop Loss">⚠️</span>}
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-green-400">
                              {pos.take_profit ? formatPrice(pos.take_profit) : <span className="text-yellow-500" title="No Take Profit">⚠️</span>}
                            </td>
                            <td className={`py-2 px-2 text-right font-mono font-semibold ${
                              pos.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                              {pos.unrealized_pnl >= 0 ? '+' : ''}${pos.unrealized_pnl.toFixed(2)}
                              <span className="text-[10px] ml-1 opacity-70">({roePct >= 0 ? '+' : ''}{roePct.toFixed(1)}%)</span>
                              {toZar(pos.unrealized_pnl) && (
                                <span className={`block text-[9px] font-mono ${pos.unrealized_pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                  {pos.unrealized_pnl >= 0 ? '+' : ''}{toZar(pos.unrealized_pnl)}
                                </span>
                              )}
                            </td>
                            <td className="py-2 px-2 text-center" onClick={e => e.stopPropagation()}>
                              <button
                                onClick={() => closeSimPosition(pos.id, pos.symbol)}
                                disabled={closingPositionId === pos.id}
                                className="text-[11px] px-2 py-0.5 rounded bg-red-600/80 hover:bg-red-500 text-white font-medium disabled:opacity-50"
                              >
                                {closingPositionId === pos.id ? '...' : 'Close'}
                              </button>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )
            ) : simTab === 'closed' ? (
              closedPositions.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-6">No closed positions yet</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-700">
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-left py-2 px-2">Type</th>
                        <th className="text-left py-2 px-2">Side</th>
                        <th className="text-right py-2 px-2">Amount</th>
                        <th className="text-right py-2 px-2">Entry</th>
                        <th className="text-right py-2 px-2">Exit</th>
                        <th className="text-center py-2 px-2">Margin</th>
                        <th className="text-center py-2 px-2">Lev</th>
                        <th className="text-right py-2 px-2">PnL</th>
                        <th className="text-right py-2 px-2">PnL %</th>
                        <th className="text-left py-2 px-2">Opened</th>
                        <th className="text-left py-2 px-2">Closed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {closedPositions.map(pos => {
                        const pnl = pos.realized_pnl ?? 0
                        const pnlPct = pos.entry_price > 0
                          ? ((pnl / (pos.entry_price * pos.amount)) * 100)
                          : 0
                        const isFutures = (pos.trade_type || 'spot') === 'futures'
                        return (
                          <tr key={pos.id} className="border-b border-gray-800 hover:bg-gray-800/40">
                            <td className="py-2 px-2 font-medium text-white">{pos.symbol}</td>
                            <td className="py-2 px-2">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                isFutures
                                  ? 'bg-orange-500/20 text-orange-400'
                                  : 'bg-blue-500/20 text-blue-400'
                              }`}>
                                {isFutures ? 'Futures' : 'Spot'}
                              </span>
                            </td>
                            <td className="py-2 px-2">
                              <span className={`font-semibold ${pos.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                                {pos.side.toUpperCase()}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{pos.amount.toFixed(6)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(pos.entry_price)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(pos.current_price)}</td>
                            <td className="py-2 px-2 text-center">
                              {isFutures ? (
                                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                  pos.margin_mode === 'isolated'
                                    ? 'bg-purple-500/20 text-purple-400'
                                    : 'bg-blue-500/20 text-blue-400'
                                }`}>
                                  {pos.margin_mode === 'isolated' ? 'Isolated' : 'Cross'}
                                </span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-center">
                              {isFutures && pos.leverage ? (
                                <span className="text-orange-300 font-mono font-semibold">{pos.leverage}x</span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className={`py-2 px-2 text-right font-mono font-semibold ${
                              pnl >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                              {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
                            </td>
                            <td className={`py-2 px-2 text-right font-mono font-semibold ${
                              pnlPct >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                              {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                            </td>
                            <td className="py-2 px-2 text-gray-500 whitespace-nowrap">{formatDateTimeZA(pos.created_at)}</td>
                            <td className="py-2 px-2 text-gray-500 whitespace-nowrap">{formatDateTimeZA(pos.closed_at)}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )
            ) : (
              simOrders.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-6">No orders yet</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-700">
                        <th className="text-left py-2 px-2">Time</th>
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-left py-2 px-2">Mode</th>
                        <th className="text-left py-2 px-2">Side</th>
                        <th className="text-left py-2 px-2">Type</th>
                        <th className="text-right py-2 px-2">Amount</th>
                        <th className="text-right py-2 px-2">Price</th>
                        <th className="text-center py-2 px-2">Margin</th>
                        <th className="text-center py-2 px-2">Lev</th>
                        <th className="text-right py-2 px-2">SL</th>
                        <th className="text-right py-2 px-2">TP</th>
                        <th className="text-center py-2 px-2">Status</th>
                        <th className="text-center py-2 px-2">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {simOrders.map(o => {
                        const oFutures = (o.trade_type || 'spot') === 'futures'
                        return (
                          <tr key={o.id} className="border-b border-gray-800 hover:bg-gray-800/40">
                            <td className="py-2 px-2 text-gray-500">{formatDateTimeZA(o.created_at)}</td>
                            <td className="py-2 px-2 font-medium text-white">{o.symbol}</td>
                            <td className="py-2 px-2">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                                oFutures
                                  ? 'bg-orange-500/20 text-orange-400'
                                  : 'bg-blue-500/20 text-blue-400'
                              }`}>
                                {oFutures ? 'Futures' : 'Spot'}
                              </span>
                            </td>
                            <td className="py-2 px-2">
                              <span className={`font-semibold ${o.side === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                                {o.side.toUpperCase()}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-gray-400">{o.order_type}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{o.amount.toFixed(6)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(o.price)}</td>
                            <td className="py-2 px-2 text-center">
                              {oFutures ? (
                                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                  o.margin_mode === 'isolated'
                                    ? 'bg-purple-500/20 text-purple-400'
                                    : 'bg-blue-500/20 text-blue-400'
                                }`}>
                                  {o.margin_mode === 'isolated' ? 'Isolated' : 'Cross'}
                                </span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-center">
                              {oFutures && o.leverage ? (
                                <span className="text-orange-300 font-mono font-semibold">{o.leverage}x</span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-red-400/70">
                              {o.stop_loss ? formatPrice(o.stop_loss) : '—'}
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-green-400/70">
                              {o.take_profit ? formatPrice(o.take_profit) : '—'}
                            </td>
                            <td className="py-2 px-2 text-center">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                                o.status === 'filled' ? 'bg-green-500/20 text-green-400'
                                  : o.status === 'open' ? 'bg-blue-500/20 text-blue-400'
                                  : 'bg-gray-500/20 text-gray-400'
                              }`}>
                                {o.status}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-center">
                              {o.status !== 'canceled' ? (
                                <button
                                  onClick={() => cancelSimOrder(o.id)}
                                  disabled={simLoading}
                                  className="px-2 py-0.5 text-[10px] font-medium rounded bg-red-600/80 text-white hover:bg-red-500 transition disabled:opacity-50"
                                >
                                  Cancel
                                </button>
                              ) : (
                                <span className="text-gray-600 text-[10px]">—</span>
                              )}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )
            )}
          </div>
        )}

        {/* ─── Live Positions & Orders ─── */}
        {isLiveMode && (
          <div className="bg-gray-800/30 border border-green-500/30 rounded-lg p-5">
            {/* Tab toggle */}
            <div className="flex items-center gap-4 mb-4">
              <button
                onClick={() => setLiveTab('positions')}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  liveTab === 'positions'
                    ? 'border-green-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Open Positions ({liveAccount?.open_positions?.length ?? 0})
              </button>
              <button
                onClick={() => { setLiveTab('closed'); fetchLiveClosedTrades() }}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  liveTab === 'closed'
                    ? 'border-green-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Closed ({liveClosedTrades.length})
              </button>
              <button
                onClick={() => { setLiveTab('orders'); fetchLiveOpenOrders() }}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  liveTab === 'orders'
                    ? 'border-green-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Open Orders ({liveOpenOrders.length})
              </button>
              <button
                onClick={() => { setLiveTab('history'); fetchLiveOrderHistory() }}
                className={`text-sm font-semibold pb-1 border-b-2 transition ${
                  liveTab === 'history'
                    ? 'border-green-400 text-white'
                    : 'border-transparent text-gray-400 hover:text-gray-200'
                }`}
              >
                Order History
              </button>
              <button
                onClick={refreshLiveData}
                className="ml-auto text-gray-400 hover:text-white"
                title="Refresh"
              >
                <RefreshCw className={`w-4 h-4 ${liveLoading ? 'animate-spin' : ''}`} />
              </button>
              {liveTab === 'positions' && (liveAccount?.open_positions?.length ?? 0) > 0 && (
                <>
                  {liveAccount!.open_positions.some(p => !p.stop_loss || !p.take_profit) && (
                    <button
                      onClick={async () => {
                        try {
                          await apiClient.backfillLiveSlTp()
                          await refreshLiveData()
                        } catch {}
                      }}
                      disabled={liveLoading}
                      className="text-xs px-3 py-1 rounded bg-yellow-600/80 hover:bg-yellow-500 text-white font-medium disabled:opacity-50 flex items-center gap-1"
                      title="Analyse and add SL/TP for positions missing them"
                    >
                      <Shield className="w-3 h-3" /> Add SL/TP
                    </button>
                  )}
                  <button
                    onClick={handleOptimizePositions}
                    disabled={optimizingPositions}
                    className="text-xs px-3 py-1.5 rounded-lg bg-violet-600/20 border border-violet-500/30 text-violet-300 hover:bg-violet-600/30 font-medium disabled:opacity-50 flex items-center gap-1 transition"
                    title="AI analyzes market conditions and recalculates SL/TP for open positions"
                  >
                    {optimizingPositions ? (
                      <><RefreshCw className="w-3 h-3 animate-spin" /> Optimizing...</>
                    ) : (
                      <><Shield className="w-3 h-3" /> AI Optimize SL/TP</>
                    )}
                  </button>
                  <button
                    onClick={closeAllLivePositions}
                    disabled={liveLoading}
                    className="text-xs px-3 py-1 rounded bg-red-600/80 hover:bg-red-500 text-white font-medium disabled:opacity-50"
                  >
                    Close All
                  </button>
                </>
              )}
            </div>

            {liveTab === 'positions' ? (
              !liveAccount || liveAccount.open_positions.length === 0 ? (
                <div className="text-center py-8 text-gray-500 text-sm">
                  No open positions on Bitget Futures
                </div>
              ) : (
            <>
            {/* AI SL/TP optimization result summary */}
            {posOptimizeResult && posOptimizeResult.reviews && posOptimizeResult.reviews.length > 0 && (
              <div className="mb-2 p-2 bg-gray-900/50 border border-violet-500/20 rounded text-[10px] space-y-1">
                {posOptimizeResult.reviews.map((r: any, i: number) => (
                  <div key={i} className="flex items-center gap-2">
                    <span className="text-gray-300 font-medium">{r.symbol}</span>
                    <span className={`uppercase font-semibold ${r.action === 'adjust' ? 'text-violet-400' : 'text-gray-500'}`}>
                      {r.action}
                    </span>
                    {r.action === 'adjust' && (
                      <span className="text-gray-400 font-mono">
                        {r.ai_new_sl ? `SL→${Number(r.ai_new_sl).toFixed(4)}` : ''}
                        {r.ai_new_sl && r.ai_new_tp ? ' ' : ''}
                        {r.ai_new_tp ? `TP→${Number(r.ai_new_tp).toFixed(4)}` : ''}
                      </span>
                    )}
                    <span className="text-gray-600 truncate flex-1">{r.reasoning?.slice(0, 80)}</span>
                  </div>
                ))}
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700">
                    <th className="text-left py-2 px-2">Pair</th>
                    <th className="text-left py-2 px-2">Side</th>
                    <th className="text-right py-2 px-2">Amount</th>
                    <th className="text-right py-2 px-2">Entry</th>
                    <th className="text-right py-2 px-2">Current</th>
                    <th className="text-center py-2 px-2">Margin</th>
                    <th className="text-center py-2 px-2">Lev</th>
                    <th className="text-right py-2 px-2">Margin Size</th>
                    <th className="text-right py-2 px-2">Liq. Price</th>
                    <th className="text-right py-2 px-2">SL</th>
                    <th className="text-right py-2 px-2">TP</th>
                    <th className="text-right py-2 px-2">PnL</th>
                    <th className="text-center py-2 px-2">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {liveAccount.open_positions.map((pos, i) => {
                    const chartSymbol = pos.symbol.replace(/USDT$/, '/USDT').replace(/USDC$/, '/USDC')
                    const posKey = `${pos.symbol}-${pos.side}`
                    return (
                      <tr
                        key={`${pos.symbol}-${pos.side}-${i}`}
                        className="border-b border-gray-800 hover:bg-gray-800/40 cursor-pointer"
                        onClick={() => setSelectedSymbol(chartSymbol)}
                      >
                        <td className="py-2 px-2 font-medium text-white underline decoration-dotted underline-offset-2">{chartSymbol}</td>
                        <td className="py-2 px-2">
                          <span className={`font-semibold ${pos.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                            {pos.side.toUpperCase()}
                          </span>
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-gray-300">{pos.amount}</td>
                        <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(Number(pos.entry_price))}</td>
                        <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(Number(pos.current_price))}</td>
                        <td className="py-2 px-2 text-center">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                            pos.margin_mode === 'isolated'
                              ? 'bg-purple-500/20 text-purple-400'
                              : 'bg-blue-500/20 text-blue-400'
                          }`}>
                            {pos.margin_mode === 'isolated' ? 'Isolated' : 'Cross'}
                          </span>
                        </td>
                        <td className="py-2 px-2 text-center">
                          <span className="text-orange-300 font-mono font-semibold">{pos.leverage}x</span>
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-gray-300">${Number(pos.margin_size).toFixed(2)}</td>
                        <td className="py-2 px-2 text-right font-mono text-red-400">{formatPrice(Number(pos.liquidation_price))}</td>
                        <td className="py-2 px-2 text-right font-mono text-red-400" onClick={e => e.stopPropagation()}>
                          {editingSlTp?.posKey === posKey && editingSlTp.field === 'sl' ? (
                            <input
                              type="number"
                              step="any"
                              autoFocus
                              className="w-20 bg-gray-900 border border-red-500 rounded px-1 py-0.5 text-xs text-red-400 font-mono text-right"
                              value={editingSlTp.value}
                              onChange={e => setEditingSlTp({ ...editingSlTp, value: e.target.value })}
                              onKeyDown={e => {
                                if (e.key === 'Enter') saveInlineSlTp(pos.symbol, pos.side, 'sl', editingSlTp.value)
                                if (e.key === 'Escape') setEditingSlTp(null)
                              }}
                              onBlur={() => saveInlineSlTp(pos.symbol, pos.side, 'sl', editingSlTp.value)}
                              disabled={savingSlTp}
                            />
                          ) : pos.stop_loss ? (
                            <div>
                              <span
                                className="cursor-pointer hover:underline hover:text-red-300"
                                title="Click to edit Stop Loss"
                                onClick={() => setEditingSlTp({ posKey, field: 'sl', value: String(pos.stop_loss) })}
                              >
                                {formatPrice(pos.stop_loss)}
                              </span>
                              {(() => {
                                const slPnl = pos.side === 'long'
                                  ? (pos.stop_loss - pos.entry_price) * pos.amount
                                  : (pos.entry_price - pos.stop_loss) * pos.amount
                                const zarStr = toZar(slPnl)
                                return zarStr ? (
                                  <span className={`block text-[9px] font-mono ${slPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                    {slPnl >= 0 ? '+' : ''}{zarStr}
                                  </span>
                                ) : null
                              })()}
                            </div>
                          ) : (
                            <span
                              className="text-yellow-500 cursor-pointer hover:text-yellow-300"
                              title="Click to set Stop Loss"
                              onClick={() => setEditingSlTp({ posKey, field: 'sl', value: '' })}
                            >⚠️</span>
                          )}
                        </td>
                        <td className="py-2 px-2 text-right font-mono text-green-400" onClick={e => e.stopPropagation()}>
                          {editingSlTp?.posKey === posKey && editingSlTp.field === 'tp' ? (
                            <input
                              type="number"
                              step="any"
                              autoFocus
                              className="w-20 bg-gray-900 border border-green-500 rounded px-1 py-0.5 text-xs text-green-400 font-mono text-right"
                              value={editingSlTp.value}
                              onChange={e => setEditingSlTp({ ...editingSlTp, value: e.target.value })}
                              onKeyDown={e => {
                                if (e.key === 'Enter') saveInlineSlTp(pos.symbol, pos.side, 'tp', editingSlTp.value)
                                if (e.key === 'Escape') setEditingSlTp(null)
                              }}
                              onBlur={() => saveInlineSlTp(pos.symbol, pos.side, 'tp', editingSlTp.value)}
                              disabled={savingSlTp}
                            />
                          ) : pos.take_profit ? (
                            <div>
                              <span
                                className="cursor-pointer hover:underline hover:text-green-300"
                                title="Click to edit Take Profit"
                                onClick={() => setEditingSlTp({ posKey, field: 'tp', value: String(pos.take_profit) })}
                              >
                                {formatPrice(pos.take_profit)}
                              </span>
                              {(() => {
                                const tpPnl = pos.side === 'long'
                                  ? (pos.take_profit - pos.entry_price) * pos.amount
                                  : (pos.entry_price - pos.take_profit) * pos.amount
                                const zarStr = toZar(tpPnl)
                                return zarStr ? (
                                  <span className={`block text-[9px] font-mono ${tpPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                    {tpPnl >= 0 ? '+' : ''}{zarStr}
                                  </span>
                                ) : null
                              })()}
                            </div>
                          ) : (
                            <span
                              className="text-yellow-500 cursor-pointer hover:text-yellow-300"
                              title="Click to set Take Profit"
                              onClick={() => setEditingSlTp({ posKey, field: 'tp', value: '' })}
                            >⚠️</span>
                          )}
                        </td>
                        <td className={`py-2 px-2 text-right font-mono font-semibold ${
                          pos.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                        }`}>
                          {pos.unrealized_pnl >= 0 ? '+' : ''}${pos.unrealized_pnl.toFixed(2)}
                          {pos.unrealized_roe_pct != null && (
                            <span className="text-[10px] ml-1 opacity-70">
                              ({pos.unrealized_roe_pct >= 0 ? '+' : ''}{pos.unrealized_roe_pct.toFixed(1)}%)
                            </span>
                          )}
                          {toZar(pos.unrealized_pnl) && (
                            <span className={`block text-[9px] font-mono ${pos.unrealized_pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                              {pos.unrealized_pnl >= 0 ? '+' : ''}{toZar(pos.unrealized_pnl)}
                            </span>
                          )}
                        </td>
                        <td className="py-2 px-2 text-center" onClick={e => e.stopPropagation()}>
                          <button
                            onClick={() => closeLivePosition(pos.symbol, pos.side)}
                            disabled={closingLiveSymbol === posKey}
                            className="text-[11px] px-2 py-0.5 rounded bg-red-600/80 hover:bg-red-500 text-white font-medium disabled:opacity-50"
                          >
                            {closingLiveSymbol === posKey ? '...' : 'Close'}
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            </>
              )
            ) : liveTab === 'closed' ? (
              liveClosedTrades.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-6">No closed trades yet</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-700">
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-left py-2 px-2">Side</th>
                        <th className="text-left py-2 px-2">Type</th>
                        <th className="text-right py-2 px-2">Amount</th>
                        <th className="text-right py-2 px-2">Entry</th>
                        <th className="text-right py-2 px-2">Exit</th>
                        <th className="text-center py-2 px-2">Margin</th>
                        <th className="text-center py-2 px-2">Lev</th>
                        <th className="text-right py-2 px-2">PnL</th>
                        <th className="text-left py-2 px-2">Opened</th>
                        <th className="text-left py-2 px-2">Closed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {liveClosedTrades.map(t => {
                        const pnl = t.pnl ?? 0
                        const holdSide = t.side === 'buy' ? 'LONG' : 'SHORT'
                        return (
                          <tr key={t.id} className="border-b border-gray-800 hover:bg-gray-800/40">
                            <td className="py-2 px-2 font-medium text-white">{t.symbol}</td>
                            <td className="py-2 px-2">
                              <span className={`font-semibold ${t.side === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
                                {holdSide}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-gray-400">{t.order_type}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{(t.amount ?? 0).toFixed(6)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(t.price ?? 0)}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(t.average_price ?? t.price ?? 0)}</td>
                            <td className="py-2 px-2 text-center">
                              {t.margin_mode ? (
                                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                                  t.margin_mode === 'isolated'
                                    ? 'bg-purple-500/20 text-purple-400'
                                    : 'bg-blue-500/20 text-blue-400'
                                }`}>
                                  {t.margin_mode === 'isolated' ? 'Isolated' : 'Cross'}
                                </span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-center">
                              {t.leverage ? (
                                <span className="text-orange-300 font-mono font-semibold">{t.leverage}x</span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className={`py-2 px-2 text-right font-mono font-semibold ${
                              pnl >= 0 ? 'text-green-400' : 'text-red-400'
                            }`}>
                              {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
                              {toZar(pnl) && (
                                <span className={`block text-[9px] font-mono ${pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                  {pnl >= 0 ? '+' : ''}{toZar(pnl)}
                                </span>
                              )}
                            </td>
                            <td className="py-2 px-2 text-gray-500 whitespace-nowrap">
                              {formatDateTimeZA(t.created_at)}
                            </td>
                            <td className="py-2 px-2 text-gray-500 whitespace-nowrap">
                              {formatDateTimeZA(t.closed_at)}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )
            ) : liveTab === 'orders' ? (
              /* Live Open Orders */
              liveOpenOrders.length === 0 ? (
                <div className="text-center py-8 text-gray-500 text-sm">
                  No open orders on Bitget Futures
                </div>
              ) : (
                <div>
                  {/* Optimize Entries Button */}
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs text-gray-400">
                      {liveOpenOrders.filter(o => (o.orderType || 'limit').toLowerCase() === 'limit').length} limit order(s) pending
                    </span>
                    <button
                      onClick={handleOptimizeLimitOrders}
                      disabled={optimizingOrders}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] font-medium rounded-lg bg-cyan-600/20 border border-cyan-500/30 text-cyan-300 hover:bg-cyan-600/30 transition disabled:opacity-50"
                      title="AI analyzes market conditions and adjusts limit order prices for better entries"
                    >
                      {optimizingOrders ? (
                        <>
                          <RefreshCw className="w-3 h-3 animate-spin" />
                          Optimizing...
                        </>
                      ) : (
                        <>
                          <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
                          AI Optimize Entries
                        </>
                      )}
                    </button>
                  </div>
                  {/* Optimize result summary */}
                  {optimizeResult && optimizeResult.reviews && optimizeResult.reviews.length > 0 && (
                    <div className="mb-2 p-2 bg-gray-900/50 border border-cyan-500/20 rounded text-[10px] space-y-1">
                      {optimizeResult.reviews.map((r: any, i: number) => (
                        <div key={i} className="flex items-center gap-2">
                          <span className="text-gray-300 font-medium">{r.symbol}</span>
                          <span className={`font-semibold ${
                            r.action === 'adjust' ? 'text-cyan-400' :
                            r.action === 'cancel' ? 'text-red-400' : 'text-gray-500'
                          }`}>
                            {r.action.toUpperCase()}
                          </span>
                          {r.action === 'adjust' && r.new_price && (
                            <span className="text-gray-400 font-mono">
                              {r.order_price?.toFixed(2)} → <span className="text-cyan-300">{Number(r.new_price).toFixed(2)}</span>
                            </span>
                          )}
                          <span className="text-gray-600 truncate flex-1">{r.reasoning?.slice(0, 80)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-700">
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-left py-2 px-2">Side</th>
                        <th className="text-center py-2 px-2">Type</th>
                        <th className="text-right py-2 px-2">Price</th>
                        <th className="text-right py-2 px-2">Size</th>
                        <th className="text-right py-2 px-2">Filled</th>
                        <th className="text-right py-2 px-2">SL</th>
                        <th className="text-right py-2 px-2">TP</th>
                        <th className="text-center py-2 px-2">Leverage</th>
                        <th className="text-center py-2 px-2">Created</th>
                        <th className="text-center py-2 px-2">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {liveOpenOrders.map((order, i) => {
                        const chartSymbol = order.symbol
                          .replace(/_UMCBL$|_DMCBL$|_CMCBL$/, '')
                          .replace(/USDT$/, '/USDT')
                          .replace(/USDC$/, '/USDC')
                        const isBuy = order.side?.toLowerCase().includes('buy')
                        return (
                          <tr
                            key={`${order.orderId}-${i}`}
                            className="border-b border-gray-800 hover:bg-gray-800/40"
                          >
                            <td className="py-2 px-2 font-medium text-white">{chartSymbol}</td>
                            <td className="py-2 px-2">
                              <span className={`font-semibold ${isBuy ? 'text-green-400' : 'text-red-400'}`}>
                                {order.side?.toUpperCase()}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-center">
                              <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-700 text-gray-300">
                                {order.orderType || 'limit'}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">
                              {formatPrice(Number(order.price))}
                            </td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{order.size}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{order.filledQty || '0'}</td>
                            <td className="py-2 px-2 text-right font-mono">
                              {(order.stopLoss || order.presetStopLossPrice) && Number(order.stopLoss || order.presetStopLossPrice) > 0
                                ? <span className="text-red-400">{formatPrice(Number(order.stopLoss || order.presetStopLossPrice))}</span>
                                : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-right font-mono">
                              {(order.takeProfit || order.presetStopSurplusPrice) && Number(order.takeProfit || order.presetStopSurplusPrice) > 0
                                ? <span className="text-green-400">{formatPrice(Number(order.takeProfit || order.presetStopSurplusPrice))}</span>
                                : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-center">
                              <span className="text-orange-300 font-mono font-semibold">{order.leverage || '—'}x</span>
                            </td>
                            <td className="py-2 px-2 text-center text-gray-500">
                              {order.createTime ? formatTimeZA(Number(order.createTime)) : '—'}
                            </td>
                            <td className="py-2 px-2 text-center">
                              <button
                                onClick={() => cancelLiveOrder(order)}
                                disabled={cancellingOrderId === order.orderId}
                                className="px-2 py-1 text-[10px] font-medium rounded bg-red-600/20 border border-red-500/30 text-red-300 hover:bg-red-600/30 transition disabled:opacity-50"
                              >
                                {cancellingOrderId === order.orderId ? (
                                  <RefreshCw className="w-3 h-3 animate-spin inline" />
                                ) : (
                                  'Cancel'
                                )}
                              </button>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                </div>
              )
            ) : (
              /* Live Order History from Bitget */
              liveOrderHistory.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-6">No order history</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-gray-400 border-b border-gray-700">
                        <th className="text-left py-2 px-2">Time</th>
                        <th className="text-left py-2 px-2">Pair</th>
                        <th className="text-left py-2 px-2">Side</th>
                        <th className="text-left py-2 px-2">Type</th>
                        <th className="text-right py-2 px-2">Price</th>
                        <th className="text-right py-2 px-2">Size</th>
                        <th className="text-right py-2 px-2">Filled</th>
                        <th className="text-center py-2 px-2">Lev</th>
                        <th className="text-center py-2 px-2">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {liveOrderHistory.map((o: any, i: number) => {
                        const chartSymbol = (o.symbol || '')
                          .replace(/_UMCBL$|_DMCBL$|_CMCBL$/, '')
                          .replace(/USDT$/, '/USDT')
                          .replace(/USDC$/, '/USDC')
                        const isBuy = (o.side || '').toLowerCase().includes('buy')
                        const status = o.state || o.status || 'unknown'
                        return (
                          <tr key={`${o.orderId || i}-${i}`} className="border-b border-gray-800 hover:bg-gray-800/40">
                            <td className="py-2 px-2 text-gray-500">
                              {o.cTime || o.createTime ? formatDateTimeZA(Number(o.cTime || o.createTime)) : '—'}
                            </td>
                            <td className="py-2 px-2 font-medium text-white">{chartSymbol}</td>
                            <td className="py-2 px-2">
                              <span className={`font-semibold ${isBuy ? 'text-green-400' : 'text-red-400'}`}>
                                {(o.side || '').toUpperCase()}
                              </span>
                            </td>
                            <td className="py-2 px-2 text-gray-400">{o.orderType || o.type || '—'}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{formatPrice(Number(o.price || o.priceAvg || 0))}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{o.size || o.sz || '—'}</td>
                            <td className="py-2 px-2 text-right font-mono text-gray-300">{o.filledQty || o.baseVolume || '0'}</td>
                            <td className="py-2 px-2 text-center">
                              {o.leverage ? (
                                <span className="text-orange-300 font-mono font-semibold">{o.leverage}x</span>
                              ) : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="py-2 px-2 text-center">
                              <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                                status === 'filled' ? 'bg-green-500/20 text-green-400'
                                  : status === 'cancelled' || status === 'canceled' ? 'bg-red-500/20 text-red-400'
                                  : 'bg-gray-500/20 text-gray-400'
                              }`}>
                                {status}
                              </span>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )
            )}
          </div>
        )}

        {/* ─── Signal-Based Execution ─── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          {/* Actionable Signals */}
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-white flex items-center gap-2">
                <Zap className="w-4 h-4 text-yellow-400" /> Signals for {selectedSymbol}
                {isSimMode && simActive && (
                  <span className="text-[10px] font-medium bg-purple-500/30 text-purple-300 px-1.5 py-0.5 rounded">
                    SIM
                  </span>
                )}
                {isLiveMode && (
                  <span className="text-[10px] font-medium bg-green-500/30 text-green-300 px-1.5 py-0.5 rounded">
                    LIVE
                  </span>
                )}
              </h3>
              <div className="flex items-center gap-3">
                {!simActive && !isLiveMode && (
                  <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={isDryRun}
                      onChange={e => setIsDryRun(e.target.checked)}
                      className="rounded bg-gray-700 border-gray-600 text-blue-500 w-3.5 h-3.5"
                    />
                    Dry Run
                  </label>
                )}
                <button
                  onClick={fetchSignals}
                  className="text-gray-400 hover:text-white"
                  title="Refresh signals"
                >
                  <RefreshCw className={`w-4 h-4 ${signalsLoading ? 'animate-spin' : ''}`} />
                </button>
              </div>
            </div>

            {!simActive && !isLiveMode && !isDryRun && (
              <div className="mb-3 flex items-center gap-2 bg-yellow-500/10 border border-yellow-500/30 rounded p-2 text-xs text-yellow-300">
                <AlertTriangle className="w-4 h-4 shrink-0" />
                <span>Live trading is enabled — orders will be placed on {selectedExchange}.</span>
              </div>
            )}

            {isLiveMode && (
              <div className="mb-3 flex items-center gap-2 bg-green-500/10 border border-green-500/30 rounded p-2 text-xs text-green-300">
                <Zap className="w-4 h-4 shrink-0" />
                <span>Live mode — signals will execute futures orders on Bitget with your configured settings.</span>
              </div>
            )}

            {isSimMode && simActive && (
              <div className="mb-3 flex items-center gap-2 bg-purple-500/10 border border-purple-500/30 rounded p-2 text-xs text-purple-300">
                <Shield className="w-4 h-4 shrink-0" />
                <span>Executing signals in simulation mode with smart stop-loss.</span>
              </div>
            )}

            {signals.length === 0 ? (
              <p className="text-sm text-gray-500 text-center py-6">
                No signals for {selectedSymbol}. Generate signals on the Signals page first.
              </p>
            ) : (
              <div className="space-y-2 max-h-[400px] overflow-y-auto">
                {signals.map(sig => {
                  const isBuy = sig.action.toLowerCase() === 'buy'
                  const isSell = sig.action.toLowerCase() === 'sell'
                  const actionColor = isBuy ? 'text-green-400' : isSell ? 'text-red-400' : 'text-yellow-400'
                  const canExecute = sig.status === 'pending' && (isBuy || isSell)
                  return (
                    <div
                      key={sig.id}
                      className="bg-gray-800/50 border border-gray-700 rounded-lg p-3 flex items-center gap-3 cursor-pointer hover:bg-gray-800/80 transition"
                      onClick={() => setSelectedSymbol(sig.symbol)}
                      title={`Open ${sig.symbol} on chart`}
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className={`font-bold text-sm ${actionColor}`}>
                            {sig.action.toUpperCase()}
                          </span>
                          <span className="text-xs text-gray-500">{sig.source}</span>
                          <span className="text-xs text-gray-600">
                            {formatTimeZA(sig.created_at)}
                          </span>
                        </div>
                        <div className="flex items-center gap-3 mt-1 text-xs text-gray-400">
                          {sig.price && <span>{formatPrice(sig.price)}</span>}
                          <span>Conf: {(sig.confidence * 100).toFixed(0)}%</span>
                          <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                            sig.status === 'pending' ? 'bg-yellow-500/20 text-yellow-400'
                              : sig.status === 'executed' ? 'bg-green-500/20 text-green-400'
                              : 'bg-gray-500/20 text-gray-400'
                          }`}>
                            {sig.status}
                          </span>
                        </div>
                      </div>
                      {canExecute && (
                        <button
                          onClick={(e) => { e.stopPropagation(); executeSignal(sig) }}
                          disabled={executingSignalId === sig.id}
                          className={`flex items-center gap-1 px-3 py-1.5 rounded text-xs font-medium transition ${
                            isBuy
                              ? 'bg-green-600 hover:bg-green-500 text-white'
                              : 'bg-red-600 hover:bg-red-500 text-white'
                          } disabled:opacity-50`}
                        >
                          {executingSignalId === sig.id ? (
                            <RefreshCw className="w-3 h-3 animate-spin" />
                          ) : (
                            <ArrowRight className="w-3 h-3" />
                          )}
                          {isLiveMode ? 'Live Execute' : isSimMode ? 'Sim Execute' : 'Execute'}
                        </button>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {/* Recent Signal Feed (all pairs) */}
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <SignalFeed />
          </div>
        </div>
      </div>
    </>
  )
}
