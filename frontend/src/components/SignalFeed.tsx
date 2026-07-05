import { useCallback, useEffect, useState } from 'react';
import { apiClient } from '@/services/api';
import { formatPrice } from '@/utils/price';
import { useTradeStore } from '@/store/useTradeStore';
import { formatTimeZA } from '@/utils/datetime';
import { useEventStream } from '@/hooks/useEventStream';

interface Signal {
  id: number;
  symbol: string;
  action: string;
  source: string;
  price: number;
  confidence: number;
  created_at: string;
  status: string;
}

export default function SignalFeed() {
  const { setSelectedSymbol } = useTradeStore();
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchSignals = useCallback(async () => {
    try {
      const response = await apiClient.getSignals({ limit: 20 });
      setSignals(response.data);
      setLoading(false);
    } catch (error) {
      console.error('Failed to fetch signals:', error);
      setLoading(false);
    }
  }, []);

  // Realtime: refetch the moment a new signal is pushed over SSE.
  const { connected } = useEventStream('signal.new', fetchSignals);

  useEffect(() => {
    fetchSignals();
    // Fallback polling only while the realtime stream is down (no regression).
    if (connected) return;
    const interval = setInterval(fetchSignals, 10000);
    return () => clearInterval(interval);
  }, [fetchSignals, connected]);

  const getActionColor = (action: string) => {
    switch (action.toLowerCase()) {
      case 'buy':
        return 'text-bullish';
      case 'sell':
        return 'text-bearish';
      default:
        return 'text-gray-400';
    }
  };

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case 'executed':
        return 'bg-green-500/20 text-green-400';
      case 'pending':
        return 'bg-yellow-500/20 text-yellow-400';
      case 'failed':
        return 'bg-red-500/20 text-red-400';
      default:
        return 'bg-gray-500/20 text-gray-400';
    }
  };

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading signals...</div>;
  }

  return (
    <div className="space-y-2">
      <h3 className="text-lg font-semibold mb-3">Recent Signals</h3>
      {signals.length === 0 ? (
        <div className="text-center py-8 text-gray-500">No signals yet</div>
      ) : (
        <div className="space-y-2 max-h-96 overflow-y-auto">
          {signals.map((signal) => (
            <div
              key={signal.id}
              className="bg-gray-800/50 border border-gray-700 rounded-lg p-3 hover:bg-gray-800 transition cursor-pointer"
              onClick={() => setSelectedSymbol(signal.symbol)}
              title={`Open ${signal.symbol} on chart`}
            >
              <div className="flex justify-between items-start">
                <div>
                  <div className="flex items-center gap-2">
                    <span className={`font-bold ${getActionColor(signal.action)}`}>
                      {signal.action.toUpperCase()}
                    </span>
                    <span className="font-mono">{signal.symbol}</span>
                  </div>
                  <div className="text-sm text-gray-400 mt-1">
                    Source: {signal.source} • Confidence: {(signal.confidence * 100).toFixed(0)}%
                  </div>
                  {signal.price && (
                    <div className="text-sm text-gray-500">
                      Price: {formatPrice(signal.price)}
                    </div>
                  )}
                </div>
                <div className="flex flex-col items-end gap-1">
                  <span className={`text-xs px-2 py-1 rounded ${getStatusColor(signal.status)}`}>
                    {signal.status}
                  </span>
                  <span className="text-xs text-gray-500">
                    {formatTimeZA(signal.created_at)}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
