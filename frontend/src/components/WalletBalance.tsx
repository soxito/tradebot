/**
 * Wallet Balance Display
 * Shows balances from all configured exchanges with P&L tracking
 */
import { useWalletBalance } from '../hooks/useWalletBalance';
import { useEffect, useState } from 'react';
import { formatTimeZA } from '@/utils/datetime';

export default function WalletBalance() {
  const { exchanges, totalBalance, isLoading, lastUpdated, refresh } = useWalletBalance(true, 30000);
  const [initialBalance, setInitialBalance] = useState<number | null>(null);
  const [pnl, setPnl] = useState<number>(0);
  const [pnlPercent, setPnlPercent] = useState<number>(0);

  // Store initial balance on first load
  useEffect(() => {
    if (!isLoading && totalBalance > 0 && initialBalance === null) {
      const stored = localStorage.getItem('tradebot_initial_balance');
      if (stored) {
        setInitialBalance(parseFloat(stored));
      } else {
        setInitialBalance(totalBalance);
        localStorage.setItem('tradebot_initial_balance', totalBalance.toString());
      }
    }
  }, [isLoading, totalBalance, initialBalance]);

  // Calculate P&L
  useEffect(() => {
    if (initialBalance !== null && totalBalance > 0) {
      const profitLoss = totalBalance - initialBalance;
      const profitLossPercent = (profitLoss / initialBalance) * 100;
      setPnl(profitLoss);
      setPnlPercent(profitLossPercent);
    }
  }, [initialBalance, totalBalance]);

  const resetPnL = () => {
    setInitialBalance(totalBalance);
    localStorage.setItem('tradebot_initial_balance', totalBalance.toString());
    setPnl(0);
    setPnlPercent(0);
  };

  if (isLoading && exchanges.length === 0) {
    return (
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold mb-4">Wallet Balance</h3>
        <div className="flex items-center justify-center py-8">
          <div className="animate-spin rounded-full h-8 w-8 border-2 border-tradebot-accent border-t-transparent"></div>
          <span className="ml-3 text-gray-400">Loading balances...</span>
        </div>
      </div>
    );
  }

  if (exchanges.length === 0) {
    return (
      <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-6">
        <h3 className="text-lg font-semibold mb-4">Wallet Balance</h3>
        <div className="text-center py-8 text-gray-400">
          <p>No configured exchanges with API credentials</p>
          <p className="text-sm mt-2">Add exchange API keys to .env file</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold">Wallet Balance</h3>
        <div className="flex gap-2">
          <button
            onClick={refresh}
            disabled={isLoading}
            className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-sm rounded border border-gray-600 transition disabled:opacity-50"
          >
            {isLoading ? '↻' : '🔄'} Refresh
          </button>
          {initialBalance !== null && (
            <button
              onClick={resetPnL}
              className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 text-sm rounded border border-gray-600 transition"
              title="Reset P&L baseline to current balance"
            >
              Reset P&L
            </button>
          )}
        </div>
      </div>

      {/* Total Balance & P&L */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="p-4 rounded-lg border border-gray-600 bg-gray-700/30">
          <div className="text-xs text-gray-400 mb-1">Total Balance</div>
          <div className="text-2xl font-bold text-tradebot-accent">
            ${totalBalance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
        </div>

        <div className={`p-4 rounded-lg border ${pnl >= 0 ? 'border-green-500 bg-green-500/10' : 'border-red-500 bg-red-500/10'}`}>
          <div className="text-xs text-gray-400 mb-1">Total P&L</div>
          <div className={`text-2xl font-bold ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {pnl >= 0 ? '+' : ''}{pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USD
          </div>
        </div>

        <div className={`p-4 rounded-lg border ${pnlPercent >= 0 ? 'border-green-500 bg-green-500/10' : 'border-red-500 bg-red-500/10'}`}>
          <div className="text-xs text-gray-400 mb-1">P&L %</div>
          <div className={`text-2xl font-bold ${pnlPercent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {pnlPercent >= 0 ? '+' : ''}{pnlPercent.toFixed(2)}%
          </div>
        </div>
      </div>

      {/* Exchange Balances */}
      <div className="space-y-3">
        {exchanges.map((ex) => (
          <div key={ex.exchange} className="border border-gray-600 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="font-semibold capitalize">{ex.exchange}</span>
                {ex.status === 'success' && (
                  <span className="text-xs px-2 py-0.5 rounded bg-green-500/20 text-green-400 border border-green-500/30">
                    Connected
                  </span>
                )}
                {ex.status === 'error' && (
                  <span className="text-xs px-2 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/30">
                    Error
                  </span>
                )}
              </div>
              <div className="flex flex-col items-end">
                <div className="text-lg font-semibold text-tradebot-accent">
                  ${ex.totalUSD.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </div>
                {ex.futuresUSD !== undefined && ex.futuresUSD > 0 && (
                  <div className="text-xs text-gray-400">
                    + ${ex.futuresUSD.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} Futures
                  </div>
                )}
              </div>
            </div>

            {ex.status === 'error' && (
              <div className="text-sm text-red-400 mb-2">
                {ex.error || 'Failed to fetch balance'}
              </div>
            )}

            {ex.status === 'success' && ex.balances.length > 0 && (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
                {ex.balances.map((bal) => (
                  <div key={bal.currency} className="text-sm bg-gray-700/30 rounded px-2 py-1.5">
                    <div className="flex items-center justify-between">
                      <span className="text-gray-400 text-xs">{bal.currency}</span>
                      {bal.used > 0 && (
                        <span className="text-xs text-yellow-400/60" title="In use (frozen/locked)">
                          🔒
                        </span>
                      )}
                    </div>
                    <div className="font-semibold">
                      {bal.total < 0.0001
                        ? bal.total.toExponential(2)
                        : bal.total.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 8 })}
                    </div>
                    <div className="text-xs text-gray-500 space-x-2">
                      <span>Free: {bal.free.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                      {bal.used > 0 && (
                        <span className="text-yellow-400/70">Used: {bal.used.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {ex.status === 'success' && ex.balances.length === 0 && (
              <div className="text-sm text-gray-400">No assets with balance</div>
            )}
          </div>
        ))}
      </div>

      {lastUpdated && (
        <div className="mt-4 text-xs text-gray-500 text-right">
          Last updated: {formatTimeZA(lastUpdated)}
        </div>
      )}
    </div>
  );
}
