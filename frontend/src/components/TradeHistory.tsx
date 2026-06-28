import { useEffect, useState } from 'react';
import { apiClient } from '@/services/api';
import { formatDateTimeZA } from '@/utils/datetime';

interface Trade {
  id: number;
  exchange: string;
  symbol: string;
  side: string;
  amount: number;
  price: number;
  total: number;
  fee?: number;
  pnl?: number;
  status: string;
  created_at: string;
}

export default function TradeHistory() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<'all' | 'executed' | 'failed'>('all');

  useEffect(() => {
    fetchTrades();
  }, []);

  const fetchTrades = async () => {
    try {
      const response = await apiClient.getTradeHistory({ limit: 50 });
      setTrades(response.data.trades || []);
      setLoading(false);
    } catch (error) {
      console.error('Failed to fetch trades:', error);
      setLoading(false);
    }
  };

  const filteredTrades = trades.filter(trade => {
    if (filter === 'all') return true;
    return trade.status.toLowerCase() === filter;
  });

  const totalPnL = trades.reduce((sum, trade) => sum + (trade.pnl || 0), 0);
  const executedCount = trades.filter(t => t.status === 'executed').length;

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading trade history...</div>;
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-4">
        <h3 className="text-lg font-semibold">Trade History</h3>
        <div className="flex gap-2">
          {(['all', 'executed', 'failed'] as const).map((status) => (
            <button
              key={status}
              onClick={() => setFilter(status)}
              className={`px-3 py-1 rounded text-sm transition ${
                filter === status
                  ? 'bg-tradebot-accent text-gray-900 font-semibold'
                  : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              }`}
            >
              {status.charAt(0).toUpperCase() + status.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="bg-gray-800/50 border border-gray-700 rounded p-3">
          <div className="text-sm text-gray-400">Total Trades</div>
          <div className="text-2xl font-bold">{trades.length}</div>
        </div>
        <div className="bg-gray-800/50 border border-gray-700 rounded p-3">
          <div className="text-sm text-gray-400">Executed</div>
          <div className="text-2xl font-bold text-green-400">{executedCount}</div>
        </div>
        <div className="bg-gray-800/50 border border-gray-700 rounded p-3">
          <div className="text-sm text-gray-400">Total P&L</div>
          <div className={`text-2xl font-bold ${totalPnL >= 0 ? 'text-bullish' : 'text-bearish'}`}>
            {totalPnL >= 0 ? '+' : ''}${totalPnL.toFixed(2)}
          </div>
        </div>
      </div>

      {/* Trade Table */}
      {filteredTrades.length === 0 ? (
        <div className="text-center py-8 text-gray-500">No trades match the current filter</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-800/50 border-b border-gray-700">
              <tr>
                <th className="text-left p-3 text-gray-400 font-medium">Time</th>
                <th className="text-left p-3 text-gray-400 font-medium">Symbol</th>
                <th className="text-left p-3 text-gray-400 font-medium">Side</th>
                <th className="text-right p-3 text-gray-400 font-medium">Amount</th>
                <th className="text-right p-3 text-gray-400 font-medium">Price</th>
                <th className="text-right p-3 text-gray-400 font-medium">Total</th>
                <th className="text-right p-3 text-gray-400 font-medium">P&L</th>
                <th className="text-center p-3 text-gray-400 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {filteredTrades.map((trade) => (
                <tr
                  key={trade.id}
                  className="border-b border-gray-800 hover:bg-gray-800/30 transition"
                >
                  <td className="p-3 text-gray-400">
                    {formatDateTimeZA(trade.created_at)}
                  </td>
                  <td className="p-3 font-mono">{trade.symbol}</td>
                  <td className={`p-3 font-semibold ${
                    trade.side === 'buy' ? 'text-bullish' : 'text-bearish'
                  }`}>
                    {trade.side.toUpperCase()}
                  </td>
                  <td className="p-3 text-right">{(trade.amount ?? 0).toFixed(6)}</td>
                  <td className="p-3 text-right">${(trade.price ?? 0).toLocaleString()}</td>
                  <td className="p-3 text-right">${((trade.total ?? (trade.amount ?? 0) * (trade.price ?? 0))).toFixed(2)}</td>
                  <td className={`p-3 text-right font-semibold ${
                    !trade.pnl ? 'text-gray-500' :
                    trade.pnl >= 0 ? 'text-bullish' : 'text-bearish'
                  }`}>
                    {trade.pnl ? `${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(2)}` : '-'}
                  </td>
                  <td className="p-3 text-center">
                    <span className={`px-2 py-1 rounded text-xs ${
                      trade.status === 'executed'
                        ? 'bg-green-500/20 text-green-400'
                        : trade.status === 'failed'
                        ? 'bg-red-500/20 text-red-400'
                        : 'bg-gray-500/20 text-gray-400'
                    }`}>
                      {trade.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
