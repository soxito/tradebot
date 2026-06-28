import { useEffect, useState } from 'react';
import { apiClient } from '@/services/api';

interface Sentiment {
  symbol: string;
  score: number;
  magnitude: number;
  sources_count: number;
  created_at: string;
}

export default function SentimentDashboard() {
  const [sentiments, setSentiments] = useState<Sentiment[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchSentiments();
    const interval = setInterval(fetchSentiments, 30000); // Refresh every 30s
    return () => clearInterval(interval);
  }, []);

  const fetchSentiments = async () => {
    try {
      const response = await apiClient.getSentiments();
      setSentiments(response.data.sentiments || []);
      setLoading(false);
    } catch (error) {
      console.error('Failed to fetch sentiments:', error);
      setLoading(false);
    }
  };

  const getSentimentLabel = (score: number) => {
    if (score > 0.05) return 'Bullish';
    if (score < -0.05) return 'Bearish';
    return 'Neutral';
  };

  const getSentimentColor = (score: number) => {
    if (score > 0.05) return 'text-bullish';
    if (score < -0.05) return 'text-bearish';
    return 'text-gray-400';
  };

  const getSentimentBar = (score: number) => {
    const percentage = Math.abs(score) * 100;
    const color = score > 0 ? 'bg-bullish' : 'bg-bearish';
    return (
      <div className="w-full h-2 bg-gray-700 rounded-full overflow-hidden">
        <div
          className={`h-full ${color} transition-all duration-300`}
          style={{ width: `${Math.min(percentage, 100)}%` }}
        />
      </div>
    );
  };

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading sentiment data...</div>;
  }

  return (
    <div>
      <h3 className="text-lg font-semibold mb-3">Market Sentiment</h3>
      {sentiments.length === 0 ? (
        <div className="text-center py-8 text-gray-500">No sentiment data available</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {sentiments.map((sentiment) => (
            <div
              key={sentiment.symbol}
              className="bg-gray-800/50 border border-gray-700 rounded-lg p-4 hover:bg-gray-800 transition"
            >
              <div className="flex justify-between items-start mb-2">
                <span className="font-mono font-bold text-lg">{sentiment.symbol}</span>
                <span className={`text-sm font-semibold ${getSentimentColor(sentiment.score)}`}>
                  {getSentimentLabel(sentiment.score)}
                </span>
              </div>
              
              {getSentimentBar(sentiment.score)}
              
              <div className="mt-2 flex justify-between text-sm">
                <span className="text-gray-400">
                  Score: {sentiment.score > 0 ? '+' : ''}{(sentiment.score * 100).toFixed(1)}%
                </span>
                <span className="text-gray-500">
                  {sentiment.sources_count} sources
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
      
      <button
        onClick={async () => {
          setLoading(true);
          try {
            await apiClient.updateSentiments();
            await fetchSentiments();
          } catch (error) {
            console.error('Failed to update sentiments:', error);
            setLoading(false);
          }
        }}
        className="mt-4 w-full bg-tradebot-accent text-gray-900 font-semibold py-2 px-4 rounded hover:opacity-80 transition"
        disabled={loading}
      >
        {loading ? 'Updating...' : 'Refresh Sentiment Data'}
      </button>
    </div>
  );
}
