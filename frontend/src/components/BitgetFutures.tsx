/**
 * Bitget Futures Balance & Trading Controls
 * Shows futures account balance, positions, and unified trading settings
 * with searchable pair selector, leverage, margin mode — all saved at once.
 */
import { useEffect, useState, useRef, useMemo } from 'react';
import { apiClient } from '../services/api';
import { formatPrice } from '../utils/price';
import { formatTimeZA } from '@/utils/datetime';

interface FuturesBalance {
  marginCoin: string;
  locked: string;
  available: string;
  crossMaxAvailable: string;
  fixedMaxAvailable: string;
  maxTransferOut: string;
  equity: string;
  usdtEquity: string;
  unrealizedPL: string;
}

interface FuturesPosition {
  marginCoin: string;
  symbol: string;
  holdSide: string;
  openDelegateSize: string;
  marginSize: string;
  available: string;
  locked: string;
  total: string;
  leverage: string;
  achievedProfits: string;
  unrealizedPL: string;
  liquidationPrice: string;
  keepMarginRate: string;
  markPrice: string;
  openPriceAvg: string;
  breakEvenPrice: string;
  totalFee: string;
  deductedFee: string;
  marginMode: string;
  posMode: string;
  unrealizedPLR: string;
  autoMargin: string;
}

interface FuturesContract {
  symbol: string;
  baseCoin: string;
  quoteCoin: string;
  symbolStatus: string;
  minTradeNum: string;
  maxTradeNum: string;
  minLever: string;
  maxLever: string;
  [key: string]: any;
}

interface PairConfig {
  id: string;
  symbol: string;
  baseCoin: string;
  marginCoin: string;
  leverage: number;
  minLever: number;
  maxLever: number;
  marginMode: 'crossed' | 'isolated';
  productType: string;
}

const STORAGE_KEY = 'tradebot_futures_pair_configs';

function loadSavedConfigs(): PairConfig[] {
  if (typeof window === 'undefined') return [];
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return [];
    const configs: PairConfig[] = JSON.parse(saved);
    // Migrate old configs missing leverage limits
    return configs.map((c) => ({
      ...c,
      minLever: c.minLever || 1,
      maxLever: c.maxLever || 125,
    }));
  } catch {
    return [];
  }
}

function saveConfigs(configs: PairConfig[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(configs));
}

export default function BitgetFutures() {
  const [productType, setProductType] = useState('USDT-FUTURES');
  const [balance, setBalance] = useState<FuturesBalance[]>([]);
  const [positions, setPositions] = useState<FuturesPosition[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  // Contracts for searchable dropdown
  const [contracts, setContracts] = useState<FuturesContract[]>([]);
  const [contractsLoading, setContractsLoading] = useState(false);

  // Pair configs (the user's configured pairs)
  const [pairConfigs, setPairConfigs] = useState<PairConfig[]>([]);

  // Load saved configs on client only (avoids hydration mismatch)
  useEffect(() => {
    setPairConfigs(loadSavedConfigs());
  }, []);

  // Add pair form
  const [searchQuery, setSearchQuery] = useState('');
  const [showDropdown, setShowDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Save state
  const [isSaving, setIsSaving] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [saveResults, setSaveResults] = useState<any[] | null>(null);

  // ─── Data Fetching ──────────────────────────────────────────

  const fetchData = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [balanceRes, positionsRes] = await Promise.all([
        apiClient.getBitgetFuturesBalance(productType),
        apiClient.getBitgetFuturesPositions(productType),
      ]);
      setBalance(balanceRes.data.balance || []);
      setPositions(positionsRes.data.positions || []);
      setLastUpdated(new Date());
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to fetch futures data');
    } finally {
      setIsLoading(false);
    }
  };

  const fetchContracts = async () => {
    setContractsLoading(true);
    try {
      const res = await apiClient.getBitgetFuturesContracts(productType);
      const data: FuturesContract[] = res.data.contracts || [];
      // Only show tradable contracts
      setContracts(data.filter((c) => c.symbolStatus === 'normal'));
    } catch (err: any) {
      console.error('Failed to fetch contracts:', err);
    } finally {
      setContractsLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    fetchContracts();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, [productType]);

  // Close dropdown on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  // ─── Searchable Pairs ──────────────────────────────────────

  const filteredContracts = useMemo(() => {
    if (!searchQuery.trim()) return contracts.slice(0, 50);
    const q = searchQuery.toUpperCase();
    return contracts
      .filter(
        (c) =>
          c.symbol.toUpperCase().includes(q) ||
          c.baseCoin.toUpperCase().includes(q)
      )
      .slice(0, 50);
  }, [contracts, searchQuery]);

  const addPair = (contract: FuturesContract) => {
    // Don't add duplicates
    if (pairConfigs.some((p) => p.symbol === contract.symbol)) {
      setSearchQuery('');
      setShowDropdown(false);
      return;
    }

    const minLev = parseInt(contract.minLever) || 1;
    const maxLev = parseInt(contract.maxLever) || 125;
    const defaultLev = Math.min(10, maxLev);

    const newConfig: PairConfig = {
      id: crypto.randomUUID(),
      symbol: contract.symbol,
      baseCoin: contract.baseCoin,
      marginCoin: productType.startsWith('USDT') ? 'USDT' : productType.startsWith('USDC') ? 'USDC' : contract.baseCoin,
      leverage: defaultLev,
      minLever: minLev,
      maxLever: maxLev,
      marginMode: 'crossed',
      productType,
    };

    const updated = [...pairConfigs, newConfig];
    setPairConfigs(updated);
    saveConfigs(updated);
    setSearchQuery('');
    setShowDropdown(false);
  };

  const removePair = (id: string) => {
    const updated = pairConfigs.filter((p) => p.id !== id);
    setPairConfigs(updated);
    saveConfigs(updated);
  };

  const updatePairConfig = (id: string, field: keyof PairConfig, value: any) => {
    const updated = pairConfigs.map((p) => (p.id === id ? { ...p, [field]: value } : p));
    setPairConfigs(updated);
    saveConfigs(updated);
  };

  // ─── Batch Save ─────────────────────────────────────────────

  const handleSaveAll = async () => {
    if (pairConfigs.length === 0) return;

    setIsSaving(true);
    setError(null);
    setSuccessMessage(null);
    setSaveResults(null);

    try {
      const res = await apiClient.applyBatchTradingSettings({
        pairs: pairConfigs.map((p) => ({
          symbol: p.symbol,
          margin_coin: p.marginCoin,
          leverage: p.leverage,
          margin_mode: p.marginMode,
          product_type: p.productType,
        })),
      });

      const data = res.data;
      setSaveResults(data.results);

      if (data.success) {
        setSuccessMessage(`All ${pairConfigs.length} pair(s) configured successfully`);
      } else {
        const failedCount = data.results.filter((r: any) => !r.success).length;
        setError(`${failedCount} pair(s) had errors — see details below`);
      }

      fetchData();
    } catch (err: any) {
      setError(err.response?.data?.detail || err.message || 'Failed to apply settings');
    } finally {
      setIsSaving(false);
    }
  };

  // ─── Computed ───────────────────────────────────────────────

  const totalEquity = balance.reduce((sum, b) => sum + parseFloat(b.usdtEquity || '0'), 0);
  const totalUnrealizedPL = balance.reduce((sum, b) => sum + parseFloat(b.unrealizedPL || '0'), 0);

  // ─── Render ─────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Bitget Futures</h2>
        <div className="flex gap-2">
          <select
            value={productType}
            onChange={(e) => setProductType(e.target.value)}
            className="px-3 py-2 bg-gray-700 border border-gray-600 rounded text-sm"
          >
            <option value="USDT-FUTURES">USDT Futures</option>
            <option value="COIN-FUTURES">Coin Futures</option>
            <option value="USDC-FUTURES">USDC Futures</option>
          </select>
          <button
            onClick={fetchData}
            disabled={isLoading}
            className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded border border-gray-600 transition disabled:opacity-50"
          >
            {isLoading ? '↻' : '🔄'} Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500 rounded-lg p-4 text-red-400">
          {error}
        </div>
      )}

      {successMessage && (
        <div className="bg-green-500/10 border border-green-500 rounded-lg p-4 text-green-400">
          {successMessage}
        </div>
      )}

      {/* Balance Summary */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold mb-4">Account Balance</h3>

        {isLoading && balance.length === 0 ? (
          <div className="text-center py-8 text-gray-400">Loading...</div>
        ) : balance.length === 0 ? (
          <div className="text-center py-8 text-gray-400">No futures balance</div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <div className="p-4 rounded-lg border border-gray-600 bg-gray-700/30">
                <div className="text-xs text-gray-400 mb-1">Total Equity (USDT)</div>
                <div className="text-2xl font-bold text-tradebot-accent">
                  ${totalEquity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </div>
              </div>

              <div className={`p-4 rounded-lg border ${totalUnrealizedPL >= 0 ? 'border-green-500 bg-green-500/10' : 'border-red-500 bg-red-500/10'}`}>
                <div className="text-xs text-gray-400 mb-1">Unrealized P&L</div>
                <div className={`text-2xl font-bold ${totalUnrealizedPL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {totalUnrealizedPL >= 0 ? '+' : ''}{totalUnrealizedPL.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT
                </div>
              </div>

              <div className="p-4 rounded-lg border border-gray-600 bg-gray-700/30">
                <div className="text-xs text-gray-400 mb-1">Available</div>
                <div className="text-2xl font-bold">
                  {balance.reduce((sum, b) => sum + parseFloat(b.available || '0'), 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {balance.map((b, idx) => (
                <div key={idx} className="border border-gray-600 rounded-lg p-3 bg-gray-700/20">
                  <div className="font-semibold mb-2">{b.marginCoin}</div>
                  <div className="space-y-1 text-sm">
                    <div className="flex justify-between">
                      <span className="text-gray-400">Equity:</span>
                      <span>{parseFloat(b.equity || '0').toFixed(4)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-400">Available:</span>
                      <span className="text-green-400">{parseFloat(b.available || '0').toFixed(4)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-gray-400">Locked:</span>
                      <span className="text-yellow-400">{parseFloat(b.locked || '0').toFixed(4)}</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Open Positions */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold mb-4">Open Positions</h3>

        {positions.length === 0 ? (
          <div className="text-center py-8 text-gray-400">No open positions</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-gray-600">
                <tr className="text-gray-400">
                  <th className="text-left py-2 px-3">Symbol</th>
                  <th className="text-left py-2 px-3">Side</th>
                  <th className="text-right py-2 px-3">Size</th>
                  <th className="text-right py-2 px-3">Leverage</th>
                  <th className="text-right py-2 px-3">Entry Price</th>
                  <th className="text-right py-2 px-3">Mark Price</th>
                  <th className="text-right py-2 px-3">Unrealized P&L</th>
                  <th className="text-right py-2 px-3">Liq. Price</th>
                  <th className="text-left py-2 px-3">Margin</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos, idx) => {
                  const unrealizedPL = parseFloat(pos.unrealizedPL || '0');
                  return (
                    <tr key={idx} className="border-b border-gray-700 hover:bg-gray-700/30">
                      <td className="py-3 px-3 font-semibold">{pos.symbol}</td>
                      <td className="py-3 px-3">
                        <span className={`px-2 py-0.5 rounded text-xs ${pos.holdSide === 'long' ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}`}>
                          {pos.holdSide.toUpperCase()}
                        </span>
                      </td>
                      <td className="py-3 px-3 text-right">{parseFloat(pos.total || '0').toFixed(4)}</td>
                      <td className="py-3 px-3 text-right">{pos.leverage}x</td>
                      <td className="py-3 px-3 text-right">{formatPrice(parseFloat(pos.openPriceAvg || pos.breakEvenPrice || '0'))}</td>
                      <td className="py-3 px-3 text-right">{formatPrice(parseFloat(pos.markPrice || '0'))}</td>
                      <td className={`py-3 px-3 text-right font-semibold ${unrealizedPL >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {unrealizedPL >= 0 ? '+' : ''}{unrealizedPL.toFixed(2)}
                      </td>
                      <td className="py-3 px-3 text-right text-yellow-400">{formatPrice(parseFloat(pos.liquidationPrice || '0'))}</td>
                      <td className="py-3 px-3">
                        <span className="px-2 py-0.5 rounded text-xs bg-gray-600">{pos.marginMode}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ─── Trading Settings ─────────────────────────────────── */}
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">Trading Settings</h3>
          <span className="text-xs text-gray-500">
            {contracts.length > 0 ? `${contracts.length} pairs available` : contractsLoading ? 'Loading pairs...' : ''}
          </span>
        </div>

        {/* Add Pair — Searchable Dropdown */}
        <div className="mb-6" ref={dropdownRef}>
          <label className="block text-sm text-gray-400 mb-2">Add Trading Pair</label>
          <div className="relative">
            <input
              ref={inputRef}
              type="text"
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                setShowDropdown(true);
              }}
              onFocus={() => setShowDropdown(true)}
              placeholder="Search pairs... (e.g., BTC, ETH, SOL)"
              className="w-full px-4 py-3 bg-gray-700 border border-gray-600 rounded-lg text-sm focus:border-tradebot-accent focus:outline-none"
            />
            <div className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </div>

            {showDropdown && (
              <div className="absolute z-50 w-full mt-1 bg-gray-800 border border-gray-600 rounded-lg shadow-xl max-h-64 overflow-y-auto">
                {contractsLoading ? (
                  <div className="p-4 text-center text-gray-400 text-sm">Loading pairs...</div>
                ) : filteredContracts.length === 0 ? (
                  <div className="p-4 text-center text-gray-400 text-sm">
                    {searchQuery ? 'No matching pairs' : 'Type to search'}
                  </div>
                ) : (
                  filteredContracts.map((c) => {
                    const alreadyAdded = pairConfigs.some((p) => p.symbol === c.symbol);
                    return (
                      <button
                        key={c.symbol}
                        onClick={() => !alreadyAdded && addPair(c)}
                        disabled={alreadyAdded}
                        className={`w-full text-left px-4 py-2.5 text-sm flex items-center justify-between transition ${
                          alreadyAdded
                            ? 'bg-gray-700/30 text-gray-500 cursor-not-allowed'
                            : 'hover:bg-gray-700 text-white'
                        }`}
                      >
                        <span>
                          <span className="font-semibold">{c.baseCoin}</span>
                          <span className="text-gray-400">/{c.quoteCoin}</span>
                          <span className="ml-2 text-xs text-gray-500">{c.symbol}</span>
                        </span>
                        <span className="flex items-center gap-2">
                          <span className="text-xs text-gray-500">{c.minLever || '1'}-{c.maxLever || '?'}x</span>
                          {alreadyAdded && (
                            <span className="text-xs text-gray-500">Added</span>
                          )}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            )}
          </div>
        </div>

        {/* Configured Pairs List */}
        {pairConfigs.length === 0 ? (
          <div className="text-center py-8 text-gray-400 border border-dashed border-gray-600 rounded-lg">
            <p className="mb-1">No pairs configured yet</p>
            <p className="text-sm text-gray-500">Search and add a pair above to get started</p>
          </div>
        ) : (
          <div className="space-y-3">
            {pairConfigs.map((pair) => {
              const result = saveResults?.find((r: any) => r.symbol === pair.symbol);
              return (
                <div
                  key={pair.id}
                  className={`border rounded-lg p-4 transition ${
                    result
                      ? result.success
                        ? 'border-green-500/50 bg-green-500/5'
                        : 'border-red-500/50 bg-red-500/5'
                      : 'border-gray-600 bg-gray-700/20'
                  }`}
                >
                  {/* Pair Header */}
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-3">
                      <span className="text-lg font-bold">{pair.baseCoin}</span>
                      <span className="text-xs text-gray-500 bg-gray-700 px-2 py-0.5 rounded">{pair.symbol}</span>
                      {result && (
                        <span className={`text-xs px-2 py-0.5 rounded ${result.success ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}`}>
                          {result.success ? 'Saved' : 'Error'}
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => removePair(pair.id)}
                      className="text-gray-500 hover:text-red-400 transition p-1"
                      title="Remove pair"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>

                  {/* Settings Row */}
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    {/* Leverage */}
                    <div>
                      <label className="block text-xs text-gray-400 mb-1">
                        Leverage <span className="text-gray-500">({pair.minLever}-{pair.maxLever}x)</span>
                      </label>
                      <div className="flex items-center gap-2">
                        <input
                          type="range"
                          min={pair.minLever}
                          max={pair.maxLever}
                          value={pair.leverage}
                          onChange={(e) => updatePairConfig(pair.id, 'leverage', parseInt(e.target.value))}
                          className="flex-1"
                        />
                        <div className="flex items-center gap-1">
                          <input
                            type="number"
                            min={pair.minLever}
                            max={pair.maxLever}
                            value={pair.leverage}
                            onChange={(e) => {
                              const v = Math.min(pair.maxLever, Math.max(pair.minLever, parseInt(e.target.value) || pair.minLever));
                              updatePairConfig(pair.id, 'leverage', v);
                            }}
                            className="w-14 px-2 py-1 bg-gray-700 border border-gray-600 rounded text-sm text-center"
                          />
                          <span className="text-xs text-gray-400">x</span>
                        </div>
                      </div>
                    </div>

                    {/* Margin Mode */}
                    <div>
                      <label className="block text-xs text-gray-400 mb-1">Margin Mode</label>
                      <div className="flex gap-1">
                        <button
                          onClick={() => updatePairConfig(pair.id, 'marginMode', 'crossed')}
                          className={`flex-1 px-3 py-1.5 rounded border text-sm transition ${
                            pair.marginMode === 'crossed'
                              ? 'bg-blue-500/20 border-blue-500 text-blue-400'
                              : 'bg-gray-700 border-gray-600 text-gray-400 hover:text-white'
                          }`}
                        >
                          Cross
                        </button>
                        <button
                          onClick={() => updatePairConfig(pair.id, 'marginMode', 'isolated')}
                          className={`flex-1 px-3 py-1.5 rounded border text-sm transition ${
                            pair.marginMode === 'isolated'
                              ? 'bg-purple-500/20 border-purple-500 text-purple-400'
                              : 'bg-gray-700 border-gray-600 text-gray-400 hover:text-white'
                          }`}
                        >
                          Isolated
                        </button>
                      </div>
                    </div>

                    {/* Margin Coin */}
                    <div>
                      <label className="block text-xs text-gray-400 mb-1">Margin Coin</label>
                      <input
                        type="text"
                        value={pair.marginCoin}
                        onChange={(e) => updatePairConfig(pair.id, 'marginCoin', e.target.value.toUpperCase())}
                        className="w-full px-3 py-1.5 bg-gray-700 border border-gray-600 rounded text-sm"
                      />
                    </div>
                  </div>

                  {/* Error details */}
                  {result && !result.success && result.errors?.length > 0 && (
                    <div className="mt-2 text-xs text-red-400 space-y-0.5">
                      {result.errors.map((err: string, i: number) => (
                        <div key={i}>• {err}</div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* Save All Button */}
        {pairConfigs.length > 0 && (
          <div className="mt-6 flex items-center justify-between">
            <p className="text-xs text-gray-500">
              {pairConfigs.length} pair{pairConfigs.length !== 1 ? 's' : ''} configured — sets leverage (both sides) + margin mode for each pair
            </p>
            <button
              onClick={handleSaveAll}
              disabled={isSaving || pairConfigs.length === 0}
              className="px-6 py-3 bg-tradebot-accent hover:bg-tradebot-accent/80 text-gray-900 rounded-lg font-bold text-sm transition disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {isSaving ? (
                <>
                  <span className="animate-spin">↻</span>
                  Applying Settings...
                </>
              ) : (
                <>Save All Settings</>
              )}
            </button>
          </div>
        )}
      </div>

      {lastUpdated && (
        <div className="text-xs text-gray-500 text-right">
          Last updated: {formatTimeZA(lastUpdated)}
        </div>
      )}
    </div>
  );
}
