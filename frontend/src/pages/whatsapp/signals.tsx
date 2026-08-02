"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/services/api";

// Resolved lazily — the desktop build picks the API port at launch.
const API_URL = () => getApiBaseUrl();

export default function WhatsAppSignalsPage({ onStatsChange }) {
  const [signals, setSignals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState({ status: "", symbol: "" });

  useEffect(() => {
    fetchSignals();
  }, [filter.status]);

  const fetchSignals = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      if (filter.status) params.append("status", filter.status);
      if (filter.symbol) params.append("symbol", filter.symbol);
      params.append("limit", "100");
      
      const res = await fetch(`${API_URL()}/plugins/whatsapp/signals?${params}`);
      const data = await res.json();
      setSignals(data);
      if (onStatsChange) onStatsChange({ active_signals: data.filter(s => s.status === "active").length });
    } catch (e) {
      console.error("Failed to fetch signals:", e);
    } finally {
      setLoading(false);
    }
  };

  const getStatusBadge = (status) => {
    const colors = {
      active: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
      filled: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
      tp_hit: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
      sl_hit: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
      closed: "bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200",
      expired: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
      cancelled: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200",
    };
    return (
      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${colors[status] || colors.active}`}>
        {status.replace("_", " ").toUpperCase()}
      </span>
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white">Parsed Signals</h2>
          <p className="text-gray-600 dark:text-gray-400">Trading signals extracted from WhatsApp messages</p>
        </div>
      </div>

      {/* Filters */}
      <div className="bg-white dark:bg-gray-800 rounded-xl p-4 shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="flex flex-wrap gap-4">
          <div className="flex-1 min-w-[200px]">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Status</label>
            <select
              value={filter.status}
              onChange={(e) => setFilter({ ...filter, status: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="">All Statuses</option>
              <option value="active">🟢 Active</option>
              <option value="filled">✅ Filled</option>
              <option value="tp_hit">🎯 TP Hit</option>
              <option value="sl_hit">🛑 SL Hit</option>
              <option value="closed">📋 Closed</option>
              <option value="expired">⏰ Expired</option>
              <option value="cancelled">❌ Cancelled</option>
            </select>
          </div>
          <div className="flex-1 min-w-[200px]">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Symbol</label>
            <input
              type="text"
              value={filter.symbol}
              onChange={(e) => setFilter({ ...filter, symbol: e.target.value })}
              placeholder="Filter by symbol (e.g., BTCUSDT)"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>
        </div>
      </div>

      {/* Signals Table */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-500">Loading signals...</div>
        ) : signals.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            <p className="text-xl mb-2">📭 No signals found</p>
            <p>Add monitored channels and wait for messages to be parsed</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-500 dark:text-gray-400 text-sm border-b border-gray-200 dark:border-gray-700">
                  <th className="pb-3 px-4">Signal</th>
                  <th className="pb-3 px-4">Channel</th>
                  <th className="pb-3 px-4">Entry / SL / TP</th>
                  <th className="pb-3 px-4">Confidence</th>
                  <th className="pb-3 px-4">Status</th>
                  <th className="pb-3 px-4">Received</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((signal) => (
                  <tr key={signal.id} className="border-b border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50">
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <span className={`font-mono font-bold ${signal.direction === "buy" || signal.direction === "long" ? "text-green-600 dark:text-green-400" : "text-red-600 dark:text-red-400"}`}>
                          {signal.direction.toUpperCase()}
                        </span>
                        <span className="font-mono text-lg text-gray-900 dark:text-white">{signal.symbol}</span>
                        {signal.leverage && (
                          <span className="px-1.5 py-0.5 text-xs bg-gray-100 dark:bg-gray-700 rounded">{signal.leverage}x</span>
                        )}
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 font-mono">{signal.channel_title}</div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="space-y-1 text-sm font-mono">
                        {signal.entry && (
                          <div className="flex items-center gap-2">
                            <span className="text-gray-500 dark:text-gray-400">Entry:</span>
                            <span className="font-medium">{signal.entry}</span>
                          </div>
                        )}
                        {signal.stop_loss && (
                          <div className="flex items-center gap-2 text-red-600 dark:text-red-400">
                            <span className="text-gray-500 dark:text-gray-400">SL:</span>
                            <span className="font-medium">{signal.stop_loss}</span>
                          </div>
                        )}
                        {signal.take_profits && signal.take_profits.length > 0 && (
                          <div className="flex items-center gap-2 text-green-600 dark:text-green-400">
                            <span className="text-gray-500 dark:text-gray-400">TPs:</span>
                            <span className="font-medium">{signal.take_profits.join(", ")}</span>
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <div className="w-24 h-2 bg-gray-200 dark:bg-gray-700 rounded overflow-hidden">
                          <div
                            className={`h-full rounded ${
                              signal.confidence >= 0.7 ? "bg-green-500" :
                              signal.confidence >= 0.5 ? "bg-yellow-500" : "bg-red-500"
                            }`}
                            style={{ width: `${Math.round(signal.confidence * 100)}%` }}
                          />
                        </div>
                        <span className="text-sm font-medium text-gray-900 dark:text-white">
                          {Math.round(signal.confidence * 100)}%
                        </span>
                      </div>
                    </td>
                    <td className="py-3 px-4">{getStatusBadge(signal.status)}</td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400 font-mono">
                      {new Date(signal.posted_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}