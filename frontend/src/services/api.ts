import axios from 'axios';
import type { AxiosRequestConfig } from 'axios';

const DEFAULT_API_BASE_URL = 'http://localhost:1448/api/v1';

export const getApiBaseUrl = () => process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_BASE_URL;

const getApiRootUrl = () => getApiBaseUrl().replace(/\/api\/v1\/?$/, '');

export const api = axios.create({
  baseURL: getApiBaseUrl(),
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 30000,
});

// Global error interceptor — handles network, timeout, and server errors
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.code === 'ECONNABORTED') {
      console.error('[API] Request timed out');
    } else if (!error.response) {
      console.error('[API] Network error — backend may be unreachable');
    } else if (error.response.status >= 500) {
      console.error(`[API] Server error ${error.response.status}:`, error.response.data?.detail || '');
    }
    return Promise.reject(error);
  }
);

// API Endpoints
export const apiClient = {
  // Health & Status
  health: () => axios.get(`${getApiRootUrl()}/health`),
  status: (config?: AxiosRequestConfig) => api.get('/status', config),
  
  // Exchanges
  getExchangesStatus: () => api.get('/exchanges/status'),
  getBalance: (exchange: string, currency?: string) => 
    api.get(`/exchanges/${exchange}/balance`, { params: { currency } }),
  getTicker: (exchange: string, symbol: string) => {
    const urlSymbol = symbol.replace('/', '');
    return api.get(`/exchanges/${exchange}/ticker/${urlSymbol}`);
  },
  getOHLCV: (
    exchange: string,
    symbol: string,
    timeframe: string = '1h',
    limit: number = 100
  ) => {
    // Remove slash from symbol for URL path (BTC/USDT -> BTCUSDT)
    const urlSymbol = symbol.replace('/', '');
    return api.get(`/exchanges/${exchange}/ohlcv/${urlSymbol}`, {
      params: { timeframe, limit }
    });
  },
  placeSpotOrder: (data: {
    exchange: string;
    symbol: string;
    side: 'buy' | 'sell';
    order_type: 'market' | 'limit';
    amount: number;
    price?: number;
  }) => api.post(`/exchanges/${data.exchange}/order`, {
    symbol: data.symbol,
    side: data.side,
    order_type: data.order_type,
    amount: data.amount,
    price: data.price,
  }),
  
  // Signals
  getSignals: (params?: { limit?: number; status?: string; symbol?: string }) =>
    api.get('/signals/', { params }),
  getSignal: (id: number) => api.get(`/signals/${id}`),
  createSignal: (data: any) => api.post('/signals/', data),
  generateSignals: (data: { symbols: string[]; timeframe?: string; exchange?: string }) =>
    api.post('/signals/generate', data),
  generateSmcSignal: (data: {
    symbol: string;
    timeframe?: string;
    exchange?: string;
    limit?: number;
    script_id?: number;
    use_ai_agents?: boolean;
    use_insights?: boolean;
    persist_signal?: boolean;
    stop_loss_pct?: number;
    take_profit_pct?: number;
    refresh_news_if_stale?: boolean;
    remove_old_signals?: boolean;
    remove_old_scope?: 'symbol_timeframe' | 'symbol' | 'all';
    entry_mode?: 'best_limit' | 'conservative' | 'balanced' | 'aggressive' | 'market' | 'custom' | 'candidate';
    selected_entry_label?: string;
    custom_entry_price?: number;
  }) => api.post('/signals/smc/generate', data),
  getSmcSignals: (params?: { limit?: number; status?: string; symbol?: string; timeframe?: string }) =>
    api.get('/signals/smc/signals', { params }),
  getSmcOverview: (params?: {
    symbol?: string;
    timeframe?: string;
    limit?: number;
    rug_limit?: number;
    sniper_limit?: number;
  }) => api.get('/signals/smc/overview', { params }),
  getSignalMonitorPairs: () => api.get('/signals/monitor-pairs/list'),
  setSignalMonitorPairs: (symbols: string[]) => api.post('/signals/monitor-pairs/set', { symbols }),
  addSignalMonitorPairs: (symbols: string[]) => api.post('/signals/monitor-pairs/add', { symbols }),
  // Custom strategies
  runMtpc: (symbol: string, exchange = 'bitget') =>
    api.get(`/signals/mtpc/${encodeURIComponent(symbol)}`, { params: { exchange } }),
  runArAtr: (symbol: string, params?: { timeframe?: string; exchange?: string; st_mult?: number; adx_threshold?: number; sl_mult?: number; trail_mult?: number }) =>
    api.get(`/signals/ar-atr/${encodeURIComponent(symbol)}`, { params }),
  removeSignalMonitorPairs: (symbols: string[]) => api.delete('/signals/monitor-pairs/remove', { data: { symbols } }),
  getSymbolAnalysis: (symbol: string, timeframe: string = '1h', exchange: string = 'bitget') => {
    const urlSymbol = symbol.replace('/', '');
    return api.get(`/signals/analysis/${urlSymbol}`, { params: { timeframe, exchange } });
  },
  
  // Sentiment
  getSentiments: () => api.get('/sentiment/'),
  getSentiment: (symbol: string) => api.get(`/sentiment/${symbol}`),
  analyzText: (text: string) => api.post('/sentiment/analyze', { text }),
  updateSentiments: () => api.post('/sentiment/update'),
  getTrendingCoins: () => api.get('/sentiment/trending/coins'),
  getEnhancedSentiments: () => api.get('/sentiment/enhanced/all'),
  getNewsArticles: (params?: { symbol?: string; source?: string; category?: string; q?: string; hours?: number; limit?: number }) =>
    api.get('/sentiment/news/articles', { params }),
  getNewsStats: () => api.get('/sentiment/news/stats'),
  getPipelineStatus: () => api.get('/sentiment/pipeline/status'),
  
  // Rug Pull Detector
  getRugPullTokens: (params?: { status?: string; limit?: number }) =>
    api.get('/rug-pulls/', { params }),
  getRugPullStats: () => api.get('/rug-pulls/stats'),
  triggerRugPullScan: () => api.post('/rug-pulls/scan'),
  analyzeRugPullToken: (tokenId: number) => api.post(`/rug-pulls/analyze/${tokenId}`),
  updateRugPullStatus: (tokenId: number, status: string) =>
    api.patch(`/rug-pulls/${tokenId}/status`, null, { params: { new_status: status } }),
  deleteRugPullToken: (tokenId: number) => api.delete(`/rug-pulls/${tokenId}`),
  
  // Sniper Loop Controls
  startSniperLoop: (interval: number = 60) =>
    api.post('/rug-pulls/sniper/start', null, { params: { interval } }),
  stopSniperLoop: () => api.post('/rug-pulls/sniper/stop'),
  getSniperStatus: () => api.get('/rug-pulls/sniper/status'),
  runSniperOnce: () => api.post('/rug-pulls/sniper/run-once'),

  // Sniper Signals & Trades
  getSniperSignals: (params?: { limit?: number }) =>
    api.get('/rug-pulls/sniper/signals', { params }),
  getSniperTrades: (params?: { limit?: number; status?: string }) =>
    api.get('/rug-pulls/sniper/trades', { params }),
  
  // Pump Monitor
  getPumpTokens: (params?: { status?: string; limit?: number }) =>
    api.get('/pump-monitor/', { params }),
  getPumpMonitorStats: () => api.get('/pump-monitor/stats'),
  triggerPumpScan: () => api.post('/pump-monitor/scan'),
  runPumpCycle: () => api.post('/pump-monitor/run-cycle'),
  deletePumpToken: (tokenId: number) => api.delete(`/pump-monitor/${tokenId}`),
  updatePumpTokenStatus: (tokenId: number, status: string) =>
    api.patch(`/pump-monitor/${tokenId}/status`, null, { params: { new_status: status } }),
  startPumpMonitor: (interval: number = 120) =>
    api.post('/pump-monitor/monitor/start', null, { params: { interval } }),
  stopPumpMonitor: () => api.post('/pump-monitor/monitor/stop'),
  getPumpMonitorStatus: () => api.get('/pump-monitor/monitor/status'),
  runPumpMonitorOnce: () => api.post('/pump-monitor/monitor/run-once'),
  
  // Trading
  evaluateSignal: (signalId: number, accountBalance?: number) =>
    api.post(`/trading/evaluate/${signalId}`, null, {
      params: { account_balance: accountBalance }
    }),
  executeTrade: (data: { signal_id: number; exchange: string; dry_run: boolean }) =>
    api.post('/trading/execute', data),
  analyzeTradingSymbol: (symbol: string, lookbackHours?: number) =>
    api.get(`/trading/analyze/${symbol}`, {
      params: { lookback_hours: lookbackHours }
    }),
  getTradeHistory: (params?: { exchange?: string; symbol?: string; limit?: number }) =>
    api.get('/trading/history', { params }),
  getTradingStatus: () => api.get('/trading/status'),

  // Bitget Native SDK
  getBitgetAccountInfo: () => api.get('/exchanges/bitget/account-info'),
  getBitgetAssets: (coin?: string) =>
    api.get('/exchanges/bitget/assets', { params: coin ? { coin } : {} }),
  getBitgetTradeFills: (symbol: string, limit: number = 100) =>
    api.get(`/exchanges/bitget/trade-fills/${symbol}`, { params: { limit } }),
  getBitgetOrderHistory: (symbol: string, limit: number = 100) =>
    api.get(`/exchanges/bitget/order-history/${symbol}`, { params: { limit } }),
  getBitgetOpenOrders: (symbol?: string) =>
    api.get('/exchanges/bitget/open-orders', { params: symbol ? { symbol } : {} }),

  // Bitget Futures
  getBitgetFuturesBalance: (productType: string = 'USDT-FUTURES') =>
    api.get('/exchanges/bitget/futures/balance', { params: { product_type: productType } }),
  getBitgetFuturesPositions: (productType: string = 'USDT-FUTURES', marginCoin?: string) =>
    api.get('/exchanges/bitget/futures/positions', {
      params: { product_type: productType, margin_coin: marginCoin },
    }),
  createBitgetFuturesOrder: (data: {
    symbol: string;
    margin_coin: string;
    side: string;
    order_type: string;
    size: string;
    price?: string;
    margin_mode?: string;
    leverage?: number;
    trade_side?: string;
    product_type?: string;
    stop_loss_pct?: number;
    take_profit_pct?: number;
  }) => api.post('/exchanges/bitget/futures/order', data),
  cancelBitgetFuturesOrder: (orderId: string, symbol: string, marginCoin: string, productType: string = 'USDT-FUTURES') =>
    api.delete(`/exchanges/bitget/futures/order/${orderId}`, {
      params: { symbol, margin_coin: marginCoin, product_type: productType },
    }),
  getBitgetFuturesOpenOrders: (productType: string = 'USDT-FUTURES', symbol?: string) =>
    api.get('/exchanges/bitget/futures/open-orders', {
      params: { product_type: productType, symbol },
    }),
  setBitgetLeverage: (data: {
    symbol: string;
    margin_coin: string;
    leverage: number;
    hold_side?: string;
    product_type?: string;
  }) => api.post('/exchanges/bitget/futures/set-leverage', data),
  setBitgetMarginMode: (data: {
    symbol: string;
    margin_coin: string;
    margin_mode: string;
    product_type?: string;
  }) => api.post('/exchanges/bitget/futures/set-margin-mode', data),
  getBitgetFuturesContracts: (productType: string = 'USDT-FUTURES') =>
    api.get('/exchanges/bitget/futures/contracts', { params: { product_type: productType } }),
  getBitgetFuturesAccountSummary: (productType: string = 'USDT-FUTURES') =>
    api.get('/exchanges/bitget/futures/account-summary', { params: { product_type: productType } }),
  // Unified account overview + all linked Bitget accounts (main + sub-accounts)
  getBitgetAccounts: () => api.get('/exchanges/bitget/accounts'),
  closeLivePosition: (data: {
    symbol: string;
    side: string;
    amount?: string;
    product_type?: string;
    margin_coin?: string;
  }) => api.post('/exchanges/bitget/futures/close-position', data),
  closeAllLivePositions: (productType: string = 'USDT-FUTURES') =>
    api.post('/exchanges/bitget/futures/close-all-positions', null, { params: { product_type: productType } }),
  getLiveTradeHistory: (limit: number = 100) =>
    api.get('/exchanges/bitget/futures/trade-history', { params: { limit } }),
  getBitgetFuturesOrderHistory: (productType: string = 'USDT-FUTURES', symbol?: string, limit: number = 50) =>
    api.get('/exchanges/bitget/futures/order-history', { params: { product_type: productType, symbol, limit } }),
  applyBatchTradingSettings: (data: {
    pairs: Array<{
      symbol: string;
      margin_coin?: string;
      leverage?: number;
      margin_mode?: string;
      product_type?: string;
    }>;
  }) => api.post('/exchanges/bitget/futures/batch-settings', data),

  // All available pairs with delisting/adjustment info
  getBitgetAvailablePairs: (quote: string = 'USDT') =>
    api.get('/exchanges/bitget/available-pairs', { params: { quote } }),

  // ─── Simulation / Paper Trading ───
  getSimAccount: () => api.get('/simulation/account'),
  toggleSimAccount: (active: boolean) =>
    api.post('/simulation/account/toggle', { active }),
  addSimFunds: (amount: number) =>
    api.post('/simulation/account/add-funds', { amount }),
  resetSimAccount: (starting_balance: number = 10000) =>
    api.post('/simulation/account/reset', { starting_balance }),
  updateSimSettings: (data: {
    auto_trade?: boolean;
    auto_trade_pairs?: string[];
    auto_trade_timeframe?: string;
    auto_trade_max_positions?: number;
    auto_trade_risk_pct?: number;
    auto_trade_mode?: string;
    auto_trade_leverage?: number;
    auto_trade_margin_mode?: string;
    auto_trade_amount_mode?: string;
    auto_trade_pine_script_id?: number;
    min_confidence?: number;
    margin_size_usdt?: number;
    min_entry_gap_pct?: number;
    min_pump_pct?: number;
  }) => api.post('/simulation/account/settings', data),
  placeSimOrder: (data: {
    symbol: string;
    side: 'buy' | 'sell';
    amount: number;
    amount_mode?: string;
    price?: number;
    order_type?: string;
    signal_id?: number;
    auto_sl?: boolean;
    trade_type?: string;
    leverage?: number;
    margin_mode?: string;
  }) => api.post('/simulation/order', data),
  getSimPositions: (status: string = 'open') =>
    api.get('/simulation/positions', { params: { status } }),
  getSimOrders: (limit: number = 50) =>
    api.get('/simulation/orders', { params: { limit } }),
  checkSimSlTp: () => api.post('/simulation/check-sl-tp'),
  backfillSimSlTp: () => api.post('/simulation/backfill-sl-tp'),
  runAutoTradeCycle: () => api.post('/simulation/auto-trade/cycle'),
  cancelSimOrder: (orderId: number) => api.post(`/simulation/orders/${orderId}/cancel`),
  closeSimPosition: (positionId: number) => api.post(`/simulation/positions/${positionId}/close`),
  closeAllSimPositions: () => api.post('/simulation/positions/close-all'),
  startAutoTradeLoop: (interval: number = 60) =>
    api.post(`/simulation/auto-trade/loop/start?interval=${interval}`),
  stopAutoTradeLoop: () => api.post('/simulation/auto-trade/loop/stop'),
  getAutoTradeLoopStatus: () => api.get('/simulation/auto-trade/loop/status'),
  getLeverageLimits: () => api.get('/simulation/leverage-limits'),

  // ─── Live Auto-Trade ───
  getLiveTradeSettings: () => api.get('/live-trade/settings'),
  updateLiveTradeSettings: (data: {
    is_active?: boolean;
    auto_trade?: boolean;
    dry_run?: boolean;
    auto_trade_pairs?: string[];
    auto_trade_timeframe?: string;
    auto_trade_max_positions?: number;
    auto_trade_risk_pct?: number;
    auto_trade_mode?: string;
    auto_trade_amount_mode?: string;
    auto_trade_leverage?: number;
    auto_trade_margin_mode?: string;
    auto_trade_pine_script_id?: number;
    max_position_size_usdt?: number;
    max_total_exposure_usdt?: number;
    enable_ai?: boolean;
    min_confidence?: number;
    margin_size_usdt?: number;
    min_entry_gap_pct?: number;
    min_pump_pct?: number;
  }) => api.post('/live-trade/settings', data),
  runLiveAutoTradeCycle: () => api.post('/live-trade/auto-trade/cycle'),
  backfillLiveSlTp: () => api.post('/live-trade/backfill-sl-tp'),
  updateLiveSlTp: (data: { symbol: string; side: string; stop_loss?: number; take_profit?: number }) =>
    api.post('/live-trade/update-sl-tp', data),
  executeLiveSignal: (signalId: number) => api.post(`/live-trade/execute-signal/${signalId}`),
  startLiveAutoTradeLoop: (interval: number = 60) =>
    api.post(`/live-trade/auto-trade/loop/start?interval=${interval}`),
  stopLiveAutoTradeLoop: () => api.post('/live-trade/auto-trade/loop/stop'),
  getLiveAutoTradeLoopStatus: () => api.get('/live-trade/auto-trade/loop/status'),
  optimizeLimitOrders: () => api.post('/live-trade/optimize-limit-orders', {}, { timeout: 120000 }),
  optimizeOpenPositions: () => api.post('/live-trade/optimize-open-positions', {}, { timeout: 120000 }),

  // ─── Strategies & Pine Scripts ───
  getStrategies: () => api.get('/strategies/'),
  createStrategy: (data: any) => api.post('/strategies/', data),
  getStrategy: (id: number) => api.get(`/strategies/${id}`),
  updateStrategy: (id: number, data: any) => api.put(`/strategies/${id}`, data),
  deleteStrategy: (id: number) => api.delete(`/strategies/${id}`),
  generatePineScript: (strategyId: number) => api.post(`/strategies/${strategyId}/generate-pinescript`),
  evaluateStrategy: (strategyId: number, data: {
    symbol: string;
    timeframe?: string;
    exchange?: string;
    limit?: number;
  }) => api.post(`/strategies/${strategyId}/evaluate`, {
    symbol: data.symbol,
    timeframe: data.timeframe || '1h',
    exchange: data.exchange || 'bitget',
    limit: data.limit || 200,
  }),
  getStrategySignal: (strategyId: number, symbol: string, timeframe?: string, exchange?: string) =>
    api.post(`/strategies/${strategyId}/signal`, null, {
      params: { symbol, timeframe: timeframe || '1h', exchange: exchange || 'bitget' },
    }),

  getPineScripts: () => api.get('/strategies/pinescripts/all'),
  createPineScript: (data: any) => api.post('/strategies/pinescripts', data),
  getPineScript: (id: number) => api.get(`/strategies/pinescripts/${id}`),
  updatePineScript: (id: number, data: any) => api.put(`/strategies/pinescripts/${id}`, data),
  deletePineScript: (id: number) => api.delete(`/strategies/pinescripts/${id}`),
  evaluatePineScript: (scriptId: number, data: {
    symbol: string;
    timeframe?: string;
    exchange?: string;
    limit?: number;
  }) => api.post(`/strategies/pinescripts/${scriptId}/evaluate`, {
    symbol: data.symbol,
    timeframe: data.timeframe || '1h',
    exchange: data.exchange || 'bitget',
    limit: data.limit || 200,
  }),

  // Multi-evaluate: evaluate multiple strategies/pine scripts at once
  evaluateMulti: (selections: string[], data: {
    symbol: string;
    timeframe?: string;
    exchange?: string;
    limit?: number;
  }) => api.post('/strategies/evaluate-multi', {
    selections,
    symbol: data.symbol,
    timeframe: data.timeframe || '1h',
    exchange: data.exchange || 'bitget',
    limit: data.limit || 200,
  }),

  // ─── AI Agents ───────────────────────────────────────────
  getAgents: () => api.get('/agents'),
  createAgent: (data: any) => api.post('/agents', data),
  updateAgent: (id: number, data: any) => api.put(`/agents/${id}`, data),
  deleteAgent: (id: number) => api.delete(`/agents/${id}`),
  toggleAgent: (id: number) => api.post(`/agents/${id}/toggle`),
  seedDefaultAgents: () => api.post('/agents/seed-defaults'),
  getAgentStatus: () => api.get('/agents/status'),
  toggleAiAgents: () => api.post('/agents/toggle'),
  analyzeSymbol: (data: { symbol: string; timeframe?: string }) =>
    api.post('/agents/analyze', { symbol: data.symbol, timeframe: data.timeframe || '1h' }),
  analyzeMultipleSymbols: (data: { symbols: string[]; timeframe?: string }) =>
    api.post('/agents/analyze-multiple', { symbols: data.symbols, timeframe: data.timeframe || '1h' }),
  getAgentDecisions: (params?: { limit?: number; symbol?: string; session_id?: string }) =>
    api.get('/agents/decisions', { params }),
  getDecisionStats: () => api.get('/agents/decisions/stats'),
  getSessionDecisions: (sessionId: string) => api.get(`/agents/decisions/${sessionId}`),
  recordDecisionOutcome: (decisionId: number, data: { outcome: string; pnl?: number }) =>
    api.patch(`/agents/decisions/${decisionId}/outcome`, data),
  recordSessionOutcome: (sessionId: string, data: { outcome: string; pnl?: number }) =>
    api.patch(`/agents/decisions/session/${sessionId}/outcome`, data),
  getLearningStats: (params?: { symbol?: string; role?: string }) =>
    api.get('/agents/learning-stats', { params }),

  // Position Monitor
  getPositionMonitorStatus: () => api.get('/agents/position-monitor/status'),
  startPositionMonitor: (interval: number = 7200) =>
    api.post('/agents/position-monitor/start', null, { params: { interval } }),
  stopPositionMonitor: () => api.post('/agents/position-monitor/stop'),
  runPositionReview: (minHoldHours: number = 2.0) =>
    api.post('/agents/position-monitor/run', null, { params: { min_hold_hours: minHoldHours } }),

  // Custom Rule-Based Agents
  toggleCustomAgents: () => api.post('/agents/custom/toggle'),
  getCustomAgentStatus: () => api.get('/agents/custom/status'),
  testCustomAgents: (symbol: string = 'BTC/USDT') =>
    api.post('/agents/custom/test', null, { params: { symbol } }),

  // AI Strategy & Chart Analysis
  generateAIStrategy: (data: { symbol?: string; timeframe?: string; trade_type?: string; risk_level?: string }) =>
    api.post('/agents/generate-strategy', data),
  improveAIStrategy: (data: { strategy: any; goals?: string }) =>
    api.post('/agents/improve-strategy', data),
  analyzeChart: (data: { symbol: string; timeframe?: string }) =>
    api.post('/agents/analyze-chart', data),

  // ── Agent Paul Plugin (PAUL loop trading) ───────────────
  agentPaul: {
    getStatus: () => api.get('/plugins/agent-paul/status'),
    getLoopInfo: () => api.get('/plugins/agent-paul/loop-info'),
    getSettings: () => api.get('/plugins/agent-paul/settings'),
    updateSettings: (data: any) => api.put('/plugins/agent-paul/settings', data),
    decide: (data: { symbol: string; timeframe?: string; trigger?: string }) =>
      api.post('/plugins/agent-paul/decide', {
        symbol: data.symbol,
        timeframe: data.timeframe,
        trigger: data.trigger || 'manual',
      }),
    getQueue: () => api.get('/plugins/agent-paul/queue'),
    getDecisions: (limit: number = 50) =>
      api.get('/plugins/agent-paul/decisions', { params: { limit } }),
    approve: (id: number) => api.post(`/plugins/agent-paul/decisions/${id}/approve`),
    reject: (id: number) => api.post(`/plugins/agent-paul/decisions/${id}/reject`),
    execute: (id: number) => api.post(`/plugins/agent-paul/decisions/${id}/execute`),
    unify: (id: number, data: { outcome: string; pnl?: number; notes?: string }) =>
      api.post(`/plugins/agent-paul/decisions/${id}/unify`, data),
    listMt5Accounts: () => api.get('/plugins/mt5/accounts'),
  },

  // ── Telegram Signal & News Plugin ───────────────────────
  telegram: {
    getStatus: () => api.get('/plugins/telegram/status'),
    getSettings: () => api.get('/plugins/telegram/settings'),
    updateSettings: (data: any) => api.put('/plugins/telegram/settings', data),
    testConnection: () => api.post('/plugins/telegram/test-connection'),

    getAuthStatus: () => api.get('/plugins/telegram/auth/status'),
    startAuth: (data: { phone_number: string }) => api.post('/plugins/telegram/auth/start', data),
    completeAuth: (data: {
      phone_number: string;
      phone_code_hash: string | null | undefined;
      code: string;
      password?: string | null;
    }) => api.post('/plugins/telegram/auth/complete', data),
    disconnectAuth: () => api.post('/plugins/telegram/auth/disconnect'),

    discoverSubscribedChannels: (params?: {
      provider?: 'auto' | 'telethon' | 'bot_api' | 'telegram_mcp';
      limit?: number;
      refresh?: boolean;
    }) => api.get('/plugins/telegram/discovery/subscribed', { params }),
    testMethods: (data?: {
      provider?: 'auto' | 'telethon' | 'bot_api' | 'telegram_mcp';
      mode?: 'binding' | 'invoke_readonly';
      refresh?: boolean;
      limit?: number;
    }) => api.post('/plugins/telegram/methods/test', data ?? {}),

    getChannels: (params?: {
      user_id?: string;
      source_kind?: 'signals' | 'news';
    }) => api.get('/plugins/telegram/channels', { params }),
    createChannel: (data: any) => api.post('/plugins/telegram/channels', data),
    updateChannel: (sourceId: number, data: any, userId?: string) =>
      api.patch(`/plugins/telegram/channels/${sourceId}`, data, {
        params: userId ? { user_id: userId } : undefined,
      }),
    deleteChannel: (sourceId: number, userId?: string) =>
      api.delete(`/plugins/telegram/channels/${sourceId}`, {
        params: userId ? { user_id: userId } : undefined,
      }),

    poll: (data: {
      user_id?: string;
      channel_source_ids?: number[];
      limit_per_channel?: number;
    }) => api.post('/plugins/telegram/poll', data),
    getMessages: (params?: {
      user_id?: string;
      source_kind?: 'signals' | 'news';
      channel_source_id?: number;
      limit?: number;
      per_channel?: boolean;
    }) => api.get('/plugins/telegram/messages', { params }),

    getSignals: (params?: {
      status?: 'active' | 'filled' | 'tp_hit' | 'sl_hit' | 'closed';
      market_type?: 'crypto' | 'forex';
      channel_source_id?: number;
      limit?: number;
    }) => api.get('/plugins/telegram/signals', { params }),
    getSignalPrices: (symbols: string[]) =>
      api.get('/plugins/telegram/prices', { params: { symbols: symbols.join(',') } }),
    executeParsedSignal: (signalId: number, mode: 'sandbox' | 'live' | 'both' = 'sandbox', force: boolean = true) =>
      api.post(`/plugins/telegram/signals/${signalId}/execute`, null, {
        params: { mode, force },
      }),
    processOutcome: (text: string, channel_id?: number) =>
      api.post('/plugins/telegram/signals/process-outcome', { text, channel_id }),
    analyzeSignal: (signalId: number) => api.post(`/plugins/telegram/signals/${signalId}/analyze`),
    getVolumeMonitor: (limit: number = 25) =>
      api.get('/plugins/telegram/signals/volume-monitor', { params: { limit } }),
    reanalyzeSkipped: () => api.post('/plugins/telegram/signals/reanalyze-skipped'),
    volumeAlert: (text: string) => api.post('/plugins/telegram/signals/volume-alert', { text }),

    getMonitorStatus: () => api.get('/plugins/telegram/monitor/status'),
    startMonitor: () => api.post('/plugins/telegram/monitor/start'),
    stopMonitor: () => api.post('/plugins/telegram/monitor/stop'),

    getSniperSettings: () => api.get('/plugins/telegram/sniper/settings'),
    updateSniperSettings: (data: any) => api.put('/plugins/telegram/sniper/settings', data),
    getSniperTrades: (params?: {
      status?: 'pending' | 'placed' | 'skipped' | 'missed' | 'failed';
      limit?: number;
    }) => api.get('/plugins/telegram/sniper/trades', { params }),
    runSniper: () => api.post('/plugins/telegram/sniper/run'),
    executeSniperTrade: (tradeId: number, mode: 'sandbox' | 'live' | 'both' = 'sandbox', force: boolean = true) =>
      api.post(`/plugins/telegram/sniper/trades/${tradeId}/execute`, null, {
        params: { mode, force },
      }),

    // ── Bot Command-Control ─────────────────────────────────────
    bot: {
      getInfo: () => api.get('/plugins/telegram/bot/info'),
      testMessage: (chatId: string, text?: string) =>
        api.post('/plugins/telegram/bot/test-message', { chat_id: chatId, text }),
      getWebhook: () => api.get('/plugins/telegram/bot/webhook'),
      setWebhook: (url: string, secretToken?: string) =>
        api.post('/plugins/telegram/bot/webhook', { url, secret_token: secretToken }),
      deleteWebhook: () => api.delete('/plugins/telegram/bot/webhook'),
      setCommands: (commands?: { command: string; description: string }[]) =>
        api.post('/plugins/telegram/bot/commands', { commands }),
      getPolling: () => api.get('/plugins/telegram/bot/polling'),
      setPolling: (enabled: boolean, aiFallbackEnabled?: boolean) =>
        api.post('/plugins/telegram/bot/polling', { enabled, ai_fallback_enabled: aiFallbackEnabled }),
      getConfig: () => api.get('/plugins/telegram/bot/config'),
      updateConfig: (data: { bot_token_override?: string; allowed_chat_ids?: string[]; ai_fallback_enabled?: boolean }) =>
        api.patch('/plugins/telegram/bot/config', data),
    },
  },

  // ── MT5 Plugin ────────────────────────────────────────
  mt5: {
    getStatus: () => api.get('/plugins/mt5/status'),
    getAccounts: () => api.get('/plugins/mt5/accounts'),
    createAccount: (data: { name: string; login: string; server: string; password: string; account_type: string }) =>
      api.post('/plugins/mt5/accounts', data),
    updateAccount: (id: number, data: { name?: string; login?: string; server?: string; password?: string; account_type?: string }) =>
      api.put(`/plugins/mt5/accounts/${id}`, data),
    deleteAccount: (id: number) => api.delete(`/plugins/mt5/accounts/${id}`),
    syncAccount: (id: number) => api.post(`/plugins/mt5/accounts/${id}/sync`),
    retestAccount: (id: number) => api.post(`/plugins/mt5/accounts/${id}/test`),
    getOrders: (accountId: number) => api.get(`/plugins/mt5/accounts/${accountId}/orders`),
    getPositions: (accountId: number) => api.get(`/plugins/mt5/accounts/${accountId}/positions`),
    getDeals: (accountId: number, params?: { limit?: number }) =>
      api.get(`/plugins/mt5/accounts/${accountId}/deals`, { params }),
    placeOrder: (accountId: number, data: any) =>
      api.post(`/plugins/mt5/accounts/${accountId}/orders`, data),
    getRiskOverview: (accountId: number) =>
      api.get(`/plugins/mt5/risk`, { params: { account_id: accountId } }),
    getChartOverlay: (accountId: number, symbol: string) =>
      api.get(`/plugins/mt5/overlays`, { params: { account_id: accountId, symbol } }),
    syncAll: () => api.post('/plugins/mt5/sync-all'),
    testConnection: (data: { name: string; login: string; server: string; password: string; account_type: string }) =>
      api.post('/plugins/mt5/test-connection', data),
    // Trading (real MT5 orders via OrderSendSafe/OrderModifySafe/OrderCloseSafe)
    sendOrder: (data: { account_id: number; symbol: string; operation: string; volume: number; price?: number; sl?: number; tp?: number; comment?: string }) =>
      api.post('/plugins/mt5/trade/order', data),
    modifyTrade: (accountId: number, ticket: number, sl?: number, tp?: number, price?: number) =>
      api.put('/plugins/mt5/trade/modify', null, { params: { account_id: accountId, ticket, sl, tp, price } }),
    closeTrade: (data: { account_id: number; ticket: number; volume?: number }) =>
      api.post('/plugins/mt5/trade/close', data),
    cancelPendingOrder: (accountId: number, ticket: number) =>
      api.delete(`/plugins/mt5/trade/cancel/${ticket}`, { params: { account_id: accountId } }),
    // Symbol info from MT5 broker
    getSymbolParams: (accountId: number, symbol: string) =>
      api.get(`/plugins/mt5/symbols/${symbol}/params`, { params: { account_id: accountId } }),
    // SMC Sniper Strategy
    smcAnalyze: (accountId: number, symbol: string, params?: { timeframe?: string; count?: number; min_rr?: number; sl_buffer_atr?: number; use_ai?: boolean }) =>
      api.get('/plugins/mt5/strategy/analyze', { params: { account_id: accountId, symbol, ...params }, timeout: 60000 }),
    smcBacktest: (data: { account_id: number; symbol: string; timeframe?: string; count?: number; min_rr?: number; sl_buffer_atr?: number; expiry_bars?: number }) =>
      api.post('/plugins/mt5/strategy/backtest', data, { timeout: 120000 }),
    smcAnalyzeData: (data: { symbol: string; timeframe?: string; min_rr?: number; max_rr?: number; sl_buffer_atr?: number; min_confidence?: number; use_ai?: boolean; account_balance?: number; risk_per_trade_pct?: number; contract_size?: number; max_total_loss?: number; daily_profit_target_pct?: number; us_session_only?: boolean; candles: { time: number; open: number; high: number; low: number; close: number; volume?: number | null }[] }) =>
      api.post('/plugins/mt5/strategy/analyze-data', data, { timeout: 60000 }),
    smcBacktestData: (data: { symbol: string; timeframe?: string; min_rr?: number; max_rr?: number; sl_buffer_atr?: number; min_confidence?: number; expiry_bars?: number; starting_balance?: number; risk_per_trade_pct?: number; contract_size?: number; recovery_enabled?: boolean; max_risk_multiplier?: number; max_total_loss?: number; daily_profit_target_pct?: number; use_ai?: boolean; candles: { time: number; open: number; high: number; low: number; close: number; volume?: number | null }[] }) =>
      api.post('/plugins/mt5/strategy/backtest-data', data, { timeout: 120000 }),
    smcPlace: (data: { account_id: number; symbol: string; side: 'buy' | 'sell'; entry: number; stop_loss: number; take_profit: number; volume: number; comment?: string }) =>
      api.post('/plugins/mt5/strategy/place', data),
    // Equity history
    getEquityHistory: (accountId: number) =>
      api.get('/plugins/mt5/equity-history', { params: { account_id: accountId } }),
    // Broker search
    brokerSearch: (name: string) =>
      api.get('/plugins/mt5/broker-search', { params: { name } }),
    // Chart data
    getCandles: (accountId: number, symbol: string, timeframe = 'H1', count = 300) =>
      api.get('/plugins/mt5/candles', { params: { account_id: accountId, symbol, timeframe, count } }),
    getPrice: (accountId: number, symbol: string) =>
      api.get('/plugins/mt5/price', { params: { account_id: accountId, symbol } }),
    getSymbols: (accountId: number) =>
      api.get('/plugins/mt5/symbols', { params: { account_id: accountId } }),
    // Groups
    getGroups: () => api.get('/plugins/mt5/groups'),
    createGroup: (data: { name: string }) => api.post('/plugins/mt5/groups', data),
    getEquityCurve: (groupId: number, params?: { window_hours?: number }) =>
      api.get(`/plugins/mt5/groups/${groupId}/equity-curve`, { params }),
    // Replay
    getReplayRuns: () => api.get('/plugins/mt5/replay'),
    createReplayRun: (data: any) => api.post('/plugins/mt5/replay', data),
    // Copy Profiles
    getCopyProfiles: () => api.get('/plugins/mt5/copy-profiles'),
    createCopyProfile: (data: any) => api.post('/plugins/mt5/copy-profiles', data),
    deleteCopyProfile: (id: number) => api.delete(`/plugins/mt5/copy-profiles/${id}`),
    getCopySimTrades: (profileId: number) =>
      api.get(`/plugins/mt5/copy-profiles/${profileId}/trades`),
    // ── Auto-Manage Loop ──
    startAutoManageLoop: (interval?: number) =>
      api.post('/plugins/mt5/auto-manage/loop/start', {}, { params: interval !== undefined ? { interval } : {} }),
    stopAutoManageLoop: () =>
      api.post('/plugins/mt5/auto-manage/loop/stop'),
    getAutoManageLoopStatus: () =>
      api.get('/plugins/mt5/auto-manage/loop/status'),
    runAutoManageCycle: () =>
      api.post('/plugins/mt5/auto-manage/cycle', {}, { timeout: 120000 }),
    analyzePositions: (accountId: number) =>
      api.post('/plugins/mt5/auto-manage/analyze-positions', {}, { params: { account_id: accountId }, timeout: 120000 }),
    applySuggestions: (suggestions: { ticket: number; account_id: number; sl?: number | null; tp?: number | null }[]) =>
      api.post('/plugins/mt5/auto-manage/apply-suggestions', { suggestions }, { timeout: 60000 }),
    // ── Autonomous Scalp Bot ──
    scalp: {
      start: (data: {
        account_id: number; symbol: string; lot_size?: number; auto_lot?: boolean;
        risk_per_trade_pct?: number; max_daily_loss_pct?: number; target_profit_pct?: number;
        recovery_enabled?: boolean; use_ai?: boolean; use_kronos?: boolean; timeframe?: string;
      }) => api.post('/plugins/mt5/scalp/start', data),
      stop: (accountId: number, symbol: string) =>
        api.post('/plugins/mt5/scalp/stop', { account_id: accountId, symbol }),
      status: (accountId: number) => api.get(`/plugins/mt5/scalp/status/${accountId}`),
      closeAll: (accountId: number) => api.post(`/plugins/mt5/scalp/close-all/${accountId}`),
      searchSymbols: (accountId: number, q: string) =>
        api.get('/plugins/mt5/scalp/symbols/search', { params: { account_id: accountId, q } }),
      sessions: (accountId: number) => api.get(`/plugins/mt5/scalp/sessions/${accountId}`),
      trades: (sessionId: number) => api.get(`/plugins/mt5/scalp/trades/${sessionId}`),
      update: (sessionId: number, data: {
        lot_size?: number; auto_lot?: boolean; risk_per_trade_pct?: number;
        max_daily_loss_pct?: number; target_profit_pct?: number;
        recovery_enabled?: boolean; use_ai?: boolean; use_kronos?: boolean;
      }) => api.patch(`/plugins/mt5/scalp/update/${sessionId}`, data),
    },
  },

  // ── AI Analyst Plugin ─────────────────────────────────
  aiAnalyst: {
    getStatus: () => api.get('/plugins/ai-analyst/status'),
    // Agents
    getAgents: () => api.get('/plugins/ai-analyst/agents'),
    createAgent: (data: any) => api.post('/plugins/ai-analyst/agents', data),
    getAgent: (id: number) => api.get(`/plugins/ai-analyst/agents/${id}`),
    updateAgent: (id: number, data: any) => api.patch(`/plugins/ai-analyst/agents/${id}`, data),
    deleteAgent: (id: number) => api.delete(`/plugins/ai-analyst/agents/${id}`),
    toggleAgent: (id: number) => api.post(`/plugins/ai-analyst/agents/${id}/toggle`),
    // Settings
    getSettings: () => api.get('/plugins/ai-analyst/settings'),
    updateSettings: (data: any) => api.patch('/plugins/ai-analyst/settings', data),
    // Analysis
    analyze: (data: { symbol: string; timeframe?: string }) =>
      api.post('/plugins/ai-analyst/analyze', data),
    proposeLimit: (data: { symbol: string; timeframe?: string }) =>
      api.post('/plugins/ai-analyst/propose-limit', data),
    placeLimit: (data: { decision_id: number }) =>
      api.post('/plugins/ai-analyst/place-limit', data),
    // Decisions
    getDecisions: (params?: { symbol?: string; limit?: number }) =>
      api.get('/plugins/ai-analyst/decisions', { params }),
    getDecision: (id: number) => api.get(`/plugins/ai-analyst/decisions/${id}`),
    // Overlay
    getOverlay: (symbol: string) => api.get(`/plugins/ai-analyst/overlay/${symbol}`),
    // Usage & router (provider dashboard)
    getAiUsage: () => api.get('/plugins/ai-analyst/ai/usage'),
    getAiUsageAgents: () => api.get('/plugins/ai-analyst/ai/usage/agents'),
    getRouterSettings: () => api.get('/plugins/ai-analyst/ai/router-settings'),
    updateRouterSettings: (data: any) => api.put('/plugins/ai-analyst/ai/router-settings', data),
    // Providers
    getProviders: () => api.get('/plugins/ai-analyst/ai/providers'),
    addProvider: (data: any) => api.post('/plugins/ai-analyst/ai/providers', data),
    updateProvider: (id: number, data: any) => api.put(`/plugins/ai-analyst/ai/providers/${id}`, data),
    deleteProvider: (id: number) => api.delete(`/plugins/ai-analyst/ai/providers/${id}`),
    testProvider: (id: number) => api.post(`/plugins/ai-analyst/ai/providers/${id}/test`),
    testAllProviders: () => api.post('/plugins/ai-analyst/ai/providers/test-all'),
    getProviderPresets: () => api.get('/plugins/ai-analyst/ai/providers/presets'),
    // Knowledge & graph
    getKnowledge: () => api.get('/plugins/ai-analyst/ai/knowledge'),
    addKnowledge: (data: { content: string; title?: string; kind?: string; symbol?: string; agent_role?: string; weight?: number; source?: string }) =>
      api.post('/plugins/ai-analyst/ai/knowledge', data),
    deleteKnowledge: (id: number) => api.delete(`/plugins/ai-analyst/ai/knowledge/${id}`),
    // JARVIS self-learning harvester — pulls from sentiment/SMC/telegram/insights
    harvestIntelligence: () => api.post('/plugins/ai-analyst/ai/harvest'),
    // Strategy synthesis from accumulated knowledge
    synthesizeStrategies: (n_strategies = 3) =>
      api.post('/plugins/ai-analyst/ai/strategies/synthesize', { n_strategies }),
    listSynthesizedStrategies: () => api.get('/plugins/ai-analyst/ai/strategies/synthesized'),
    getGraphOverview: () => api.get('/plugins/ai-analyst/ai/graph/overview'),
    queryGraph: (query: string, limit?: number) => api.get('/plugins/ai-analyst/ai/graph/query', { params: { term: query, limit: limit ?? 8 } }),
    // Full graph (for visualization)
    getGraphFull: () => api.get('/plugins/ai-analyst/ai/graph/full'),
    getActiveNodes: (window?: number) => api.get('/plugins/ai-analyst/ai/graph/active-nodes', { params: window ? { window } : undefined }),
    // Headroom
    getHeadroom: (days?: number) => api.get('/plugins/ai-analyst/ai/headroom', { params: days ? { days } : undefined }),
  },

  // ── Agent Paul — JARVIS chat + MT5 monitor ────────────────────────────
  jarvis: {
    /** Stream JARVIS SSE response; caller handles the ReadableStream */
    chatStream: (messages: {role: string; content: string}[], pathname?: string, sessionKey?: string): Promise<Response> =>
      fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/plugins/agent-paul/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages, pathname: pathname || '/', session_key: sessionKey || 'default' }),
      }),
    getHistory: (sessionKey: string) =>
      api.get('/plugins/agent-paul/chat/history', { params: { session_key: sessionKey } }),
    newChat: (sessionKey: string) =>
      api.post('/plugins/agent-paul/chat/new', null, { params: { session_key: sessionKey } }),
    getNews: (topic?: string, symbol?: string) =>
      api.get('/plugins/agent-paul/news', { params: { ...(topic ? { topic } : {}), ...(symbol ? { symbol } : {}) } }),
    ingestNews: () => api.post('/plugins/agent-paul/news/ingest'),
    predict: (pair: string) =>
      api.get('/plugins/agent-paul/predict', { params: { pair } }),
    searchKnowledge: (q: string, symbol?: string) =>
      api.get('/plugins/agent-paul/knowledge/search', { params: { q, ...(symbol ? { symbol } : {}) } }),
    knowledgeStats: () => api.get('/plugins/agent-paul/knowledge/stats'),
    research: (url: string) => api.post('/plugins/agent-paul/research', null, { params: { url } }),
    getAlerts: (unreadOnly?: boolean) =>
      api.get('/plugins/agent-paul/alerts', { params: unreadOnly ? { unread_only: true } : {} }),
    /** Call the real Jarvis command backend — bypasses AI chat to avoid hallucination. */
    executeCommand: (command: string, exchange?: string) =>
      api.post('/jarvis/command', { command, exchange: exchange || null }),
    /** Unified real-time monitor — all crypto positions + MT5 accounts/balances + grand totals.
     *  sync=true pulls live MT5 balance/positions from the bridge (slower); default is the fast cached read. */
    unifiedMonitor: (sync?: boolean) =>
      api.get('/jarvis/unified-monitor', { params: sync ? { sync: true } : {} }),
    /** AI/SMC analysis of an MT5 account's open positions (SL/TP suggestions). */
    analyzePositions: (accountId: number) =>
      api.get('/jarvis/analyze-positions', { params: { account_id: accountId } }),
    /** Live host CPU / RAM / load stats for the JARVIS Room system HUD. */
    systemStats: () => api.get('/jarvis/system-stats'),
    /** Searchable crypto-pair catalog (symbol, name, market cap, 24h volume). */
    pairs: (q?: string, limit = 50) =>
      api.get('/jarvis/pairs', { params: { q, limit } }),
    /** Compact { symbol: name } map (keyed by BTC/USDT AND BTCUSDT). */
    pairNames: () => api.get('/jarvis/pairs/names'),
    /** Resolve a token name/ticker/symbol to a tradeable Bitget pair + live metadata. */
    resolvePair: (q: string) =>
      api.get('/jarvis/pairs/resolve', { params: { q } }),
    /** Persist voice fingerprint + vocabulary to the permanent vault brain. */
    voiceBrainSync: (data: {
      vocabulary: Record<string, number>;
      profile?: { bands: number[]; bandStdDev?: number[]; centroid: number; sessions?: number };
      sessions?: number;
    }) => api.post('/jarvis/voice-brain/sync', data),
    /** Restore voice fingerprint + vocabulary from the permanent vault brain. */
    voiceBrainLoad: () => api.get('/jarvis/voice-brain/load'),
    /** Voice binary engine — identify speaker by frequency bands (returns confidence 0–1). */
    voiceBrainIdentify: (bands: number[], centroid?: number) =>
      api.post('/jarvis/voice-brain/identify', { bands, centroid }),
    markAlertRead: (id: string) => api.patch(`/plugins/agent-paul/alerts/${id}/read`),
    clearAlerts: () => api.delete('/plugins/agent-paul/alerts'),
    // ── JARVIS brain (OpenHuman-inspired: Memory Tree, Goals/Todos, Idle) ──
    memoryStats: () => api.get('/plugins/agent-paul/jarvis/memory/stats'),
    memoryNew: (sinceMinutes?: number, minImportance?: number) =>
      api.get('/plugins/agent-paul/jarvis/memory/new', {
        params: { ...(sinceMinutes ? { since_minutes: sinceMinutes } : {}),
                  ...(minImportance != null ? { min_importance: minImportance } : {}) },
      }),
    memoryRollup: () => api.post('/plugins/agent-paul/jarvis/memory/rollup'),
    autoFetchStatus: () => api.get('/plugins/agent-paul/jarvis/auto-fetch/status'),
    autoFetchRun: () => api.post('/plugins/agent-paul/jarvis/auto-fetch/run'),
    getGoals: (sessionKey?: string, includeDone = true, scope?: string) =>
      api.get('/plugins/agent-paul/jarvis/goals', {
        params: { ...(sessionKey ? { session_key: sessionKey } : {}),
                  ...(scope ? { scope } : {}), include_done: includeDone },
      }),
    createGoal: (data: { title: string; detail?: string; session_key?: string; scope?: string; priority?: number; token_budget?: number }) =>
      api.post('/plugins/agent-paul/jarvis/goals', data),
    updateGoal: (id: number, data: any) => api.patch(`/plugins/agent-paul/jarvis/goals/${id}`, data),
    deleteGoal: (id: number) => api.delete(`/plugins/agent-paul/jarvis/goals/${id}`),
    /** Reflect agent — reviews long-term goals vs memory, bootstraps if empty. */
    reflectGoals: () => api.post('/plugins/agent-paul/jarvis/goals/reflect'),
    getTodos: (sessionKey?: string, goalId?: number) =>
      api.get('/plugins/agent-paul/jarvis/todos', {
        params: { ...(sessionKey ? { session_key: sessionKey } : {}), ...(goalId != null ? { goal_id: goalId } : {}) },
      }),
    createTodo: (data: { title: string; goal_id?: number; session_key?: string; status?: string; detail?: string; needs_approval?: boolean }) =>
      api.post('/plugins/agent-paul/jarvis/todos', data),
    updateTodo: (id: number, data: any) => api.patch(`/plugins/agent-paul/jarvis/todos/${id}`, data),
    deleteTodo: (id: number) => api.delete(`/plugins/agent-paul/jarvis/todos/${id}`),
    idleStatus: () => api.get('/plugins/agent-paul/jarvis/idle-status'),
    idleRun: (sessionKey?: string) =>
      api.post('/plugins/agent-paul/jarvis/idle-run', { session_key: sessionKey || 'default' }),
    // Subconscious heartbeat (OpenHuman-style "keeps thinking")
    subconsciousStatus: () => api.get('/plugins/agent-paul/jarvis/subconscious/status'),
    subconsciousTick: () => api.post('/plugins/agent-paul/jarvis/subconscious/tick'),
    activityFeed: (limit = 30) =>
      api.get('/plugins/agent-paul/jarvis/activity', { params: { limit } }),
    startMt5Monitor: () => api.post('/plugins/agent-paul/mt5-monitor/start'),
    stopMt5Monitor: () => api.post('/plugins/agent-paul/mt5-monitor/stop'),
    getMt5MonitorStatus: () => api.get('/plugins/agent-paul/mt5-monitor/status'),
    // Skills
    listSkills: () => api.get('/plugins/agent-paul/skills'),
    createSkill: (data: any) => api.post('/plugins/agent-paul/skills', data),
    updateSkill: (id: number, data: any) => api.put(`/plugins/agent-paul/skills/${id}`, data),
    deleteSkill: (id: number) => api.delete(`/plugins/agent-paul/skills/${id}`),
    // Hooks
    listHooks: () => api.get('/plugins/agent-paul/hooks'),
    createHook: (data: any) => api.post('/plugins/agent-paul/hooks', data),
    updateHook: (id: number, data: any) => api.put(`/plugins/agent-paul/hooks/${id}`, data),
    deleteHook: (id: number) => api.delete(`/plugins/agent-paul/hooks/${id}`),
    // Voice Trade Execution
    voiceTrade: (command: string) => api.post('/plugins/agent-paul/voice-trade', { command }),
    // Natural-language intent parser (hands-free AI fallback for voice commands)
    parseIntent: (text: string, pathname?: string) =>
      api.post('/plugins/agent-paul/intent', { text, pathname: pathname || '/' }),
    // JARVIS self-improvement (store learnings, update agents, record patterns)
    jarvisImprove: (data: {
      type: 'knowledge' | 'agent_update' | 'rule' | 'pattern';
      content: string;
      symbol?: string;
      agent_role?: string;
      importance?: number;
      topic?: string;
    }) => api.post('/plugins/agent-paul/jarvis-improve', data),
  },

  // ── Deepgram Voice Agent ─────────────────────────────────────────────────
  deepgram: {
    /** Returns a short-lived JWT string for the browser AgentSession tokenFactory */
    getToken: (): Promise<string> =>
      api.get('/voice/deepgram/token', { responseType: 'text' }).then((r) => r.data as string),
    /** Test if the configured API key is valid */
    testKey: () => api.get('/voice/deepgram/token', { responseType: 'text' }),
    /** Deepgram account/profile + key health (project, email, credits) */
    status: () => api.get('/voice/deepgram/status'),
    /**
     * Cost-aware fallback: send a short buffered audio clip of a missed
     * utterance for one-shot Deepgram pre-recorded STT. The backend budget
     * guard may silently decline (used_deepgram=false) when the cap is hit.
     */
    sttFallback: (clip: Blob) => {
      const form = new FormData();
      const ext = (clip.type || 'audio/webm').includes('ogg') ? 'ogg' : 'webm';
      form.append('file', clip, `jarvis-miss.${ext}`);
      return api.post('/voice/deepgram/stt', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      }).then((r) => r.data as {
        used_deepgram: boolean;
        text?: string;
        confidence?: number;
        reason?: string;
        budget?: any;
      });
    },
    /** Monthly/daily spend, caps, remaining budget and projected runway. */
    usage: () => api.get('/voice/deepgram/usage').then((r) => r.data as {
      month_spend: number; day_spend: number; monthly_cap: number; daily_cap: number;
      remaining: number; total_spend: number; total_remaining: number;
      projected_runway_days: number | null; fallback_enabled: boolean;
    }),
  },

  // ── Obsidian Knowledge Vault ─────────────────────────────────────────────
  // ─── Market Overview ──────────────────────────────────────────────────────
  getMarketOverview: () => api.get('/market/overview'),
  getMarketHistory:  (days = 30) => api.get('/market/history', { params: { days } }),

  obsidian: {
    status: () => api.get('/plugins/obsidian-knowledge/status'),
    listNotes: (params?: { note_type?: string; symbol?: string; limit?: number; offset?: number }) =>
      api.get('/plugins/obsidian-knowledge/notes', { params }),
    getNote: (path: string) =>
      api.get('/plugins/obsidian-knowledge/notes/content', { params: { path } }),
    createNote: (data: { path: string; content: string; note_type?: string; symbol?: string; tags?: string[] }) =>
      api.post('/plugins/obsidian-knowledge/notes', data),
    sync: (data?: { export_decisions?: boolean; export_signals?: boolean; export_communities?: boolean; limit?: number }) =>
      api.post('/plugins/obsidian-knowledge/sync', data ?? {}),
    graph: () => api.get('/plugins/obsidian-knowledge/graph'),
    search: (data: { query: string; note_type?: string; symbol?: string; limit?: number }) =>
      api.post('/plugins/obsidian-knowledge/search', data),
    getContext: (symbol: string) => api.get(`/plugins/obsidian-knowledge/context/${symbol}`),
    openInObsidian: (path: string) =>
      api.post('/plugins/obsidian-knowledge/open', null, { params: { path } }),
    jarvisLearn: (data: { question: string; answer: string; page?: string; tags?: string[] }) =>
      api.post('/plugins/obsidian-knowledge/jarvis-learn', data),
    signalsOverlay: () => api.get('/plugins/obsidian-knowledge/signals-overlay'),
    insightsHarvest: (data: {
      decisions?: any[]; news_articles?: any[]; sentiments?: any[];
      learning_stats?: any; pipeline_status?: any; paul_knowledge_stats?: any;
    }) => api.post('/plugins/obsidian-knowledge/insights/harvest', data),
    learningActivity: () => api.get('/plugins/obsidian-knowledge/learning-activity'),
    liveFeed: (limit?: number) => api.get('/plugins/obsidian-knowledge/live-feed', { params: limit ? { limit } : {} }),
  },

  // ── Kronos Forecast Plugin (K-line foundation model) ─────────────────────
  kronos: {
    /** Model status: real Kronos loaded vs heuristic fallback, device, etc. */
    status: () => api.get('/plugins/kronos/status'),
    /** Published model catalogue + currently active model (with install status). */
    models: () => api.get('/plugins/kronos/models'),
    /** Download every published Kronos model + tokenizer into the local cache
     *  (runs in the background on the server; poll models() for progress). */
    installAllModels: () => api.post('/plugins/kronos/models/install-all', null, { timeout: 180000 }),
    /** Hot-swap to a different Kronos variant (triggers a reload on the backend). */
    switchModel: (modelId: string) => api.post('/plugins/kronos/model/switch', null, { params: { model_id: modelId }, timeout: 180000 }),
    /** Full forecast: predicted candles + confidence bands + signal + overlays.
     *  Longer timeout — the Kronos model can cold-start for ~45–60s. */
    forecast: (
      exchange: string,
      symbol: string,
      params?: { timeframe?: string; lookback?: number; pred_len?: number; samples?: number; temperature?: number; top_p?: number },
    ) => api.get(`/plugins/kronos/forecast/${exchange}/${symbol.replace('/', '')}`, { params, timeout: 120000 }),
    /** Lightweight overlay payload (overlays + markers + signal) for any chart. */
    overlay: (
      exchange: string,
      symbol: string,
      params?: { timeframe?: string; pred_len?: number; samples?: number },
    ) => api.get(`/plugins/kronos/overlay/${exchange}/${symbol.replace('/', '')}`, { params, timeout: 120000 }),
    /** Compact speech-friendly forecast for JARVIS. */
    jarvis: (symbol: string, params?: { exchange?: string; timeframe?: string; pred_len?: number }) =>
      api.get(`/plugins/kronos/jarvis/${symbol.replace('/', '')}`, { params, timeout: 120000 }),
    /** JARVIS detailed analysis of the forecast + crypto market cap; learns each run into the brain. */
    analyze: (
      exchange: string,
      symbol: string,
      params?: { timeframe?: string; pred_len?: number; samples?: number; temperature?: number; learn?: boolean },
    ) => api.post(`/plugins/kronos/analyze/${exchange}/${symbol.replace('/', '')}`, null, { params, timeout: 120000 }),
    /** Executable sniper entries (entry / stop / take-profits / R:R + reasons) derived
     *  from the Kronos forecast direction and confidence bands. */
    sniper: (
      exchange: string,
      symbol: string,
      params?: { timeframe?: string; pred_len?: number; samples?: number },
    ) => api.get(`/plugins/kronos/sniper/${exchange}/${symbol.replace('/', '')}`, { params, timeout: 120000 }),
    /** Live account context for placing a sniper order: openable margin, max
     *  leverage, min size/precision, existing position, and max-open-remaining. */
    tradability: (exchange: string, symbol: string) =>
      api.get(`/plugins/kronos/tradability/${exchange}/${symbol.replace('/', '')}`, { timeout: 30000 }),
    /** Batch forecast several symbols at once. */
    batch: (data: { exchange?: string; timeframe?: string; symbols: string[]; lookback?: number; pred_len?: number; samples?: number }) =>
      api.post('/plugins/kronos/batch', data),
  },
};

export default apiClient;
