"use client";

import { useEffect, useState } from "react";
import { useTranslation } from "next-i18next";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:1448/api/v1";

export default function WhatsAppSniperPage({ onStatsChange }) {
  const { t } = useTranslation("common");
  const [settings, setSettings] = useState({
    enabled: false,
    mode: "sandbox",
    trade_type: "market",
    position_size_usdt: 100,
    max_positions: 5,
    max_positions_sandbox: 5,
    max_positions_live: 3,
    leverage: 10,
    margin_mode: "crossed",
    sniper_offset_pct: 0.5,
    min_confidence: 0.65,
    min_risk_reward: 1.5,
    pending_ttl_minutes: 30,
    reanalyze: true,
    execute_sandbox: true,
    execute_live: false,
    require_ai_confirmation: true,
    execute_immediately: true,
    skipped_reanalyze_minutes: 15,
    tp_trail_pct: 1.5,
  });
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState({ type: "", text: "" });

  useEffect(() => {
    fetchSettings();
    fetchTrades();
  }, []);

  const fetchSettings = async () => {
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/sniper/settings`);
      const data = await res.json();
      setSettings(data);
    } catch (e) {
      console.error("Failed to fetch settings:", e);
    }
  };

  const fetchTrades = async () => {
    try {
      setLoading(true);
      const res = await fetch(`${API_URL}/plugins/whatsapp/sniper/trades?limit=50`);
      const data = await res.json();
      setTrades(data);
      if (onStatsChange) onStatsChange({ sniper_trades: data.length });
    } catch (e) {
      console.error("Failed to fetch trades:", e);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage({ type: "", text: "" });
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/sniper/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (res.ok) {
        setMessage({ type: "success", text: "Settings saved!" });
      } else {
        setMessage({ type: "error", text: "Failed to save" });
      }
    } catch (e) {
      setMessage({ type: "error", text: "Failed to save" });
    } finally {
      setSaving(false);
    }
  };

  const handleRunSniper = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/sniper/run`, { method: "POST" });
      const data = await res.json();
      setMessage({ type: "success", text: `Sniper cycle completed` });
      fetchTrades();
    } catch (e) {
      setMessage({ type: "error", text: "Failed to run sniper" });
    } finally {
      setLoading(false);
    }
  };

  const handleExecuteTrade = async (tradeId, mode) => {
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/sniper/trades/${tradeId}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode, force: true }),
      });
      const data = await res.json();
      if (data.success) {
        setMessage({ type: "success", text: "Trade executed!" });
        fetchTrades();
      } else {
        setMessage({ type: "error", text: data.error || "Failed to execute" });
      }
    } catch (e) {
      setMessage({ type: "error", text: "Failed to execute trade" });
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white">Sniper Auto-Trade</h2>
          <p className="text-gray-600 dark:text-gray-400">Automatically execute trades from WhatsApp signals</p>
        </div>
        <div className="flex gap-3">
          <button
            onClick={handleRunSniper}
            disabled={loading}
            className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
          >
            🎯 Run Sniper Now
          </button>
        </div>
      </div>

      {/* Settings Form */}
      <div className="bg-white dark:bg-gray-800 rounded-xl p-6 shadow-sm border border-gray-200 dark:border-gray-700">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-6">Configuration</h3>

        {message.text && (
          <div className={`mb-6 p-4 rounded-lg ${
            message.type === "success" ? "bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200" :
            "bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200"
          }`}>
            {message.text}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Auto-Trading Enabled
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.enabled}
                onChange={(e) => setSettings({ ...settings, enabled: e.target.checked })}
                className="w-5 h-5 text-blue-600 border-gray-300 rounded"
              />
              <span className="text-sm text-gray-700 dark:text-gray-300">Enable sniper</span>
            </label>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Mode</label>
            <select
              value={settings.mode}
              onChange={(e) => setSettings({ ...settings, mode: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="sandbox">Sandbox (Test)</option>
              <option value="live">Live (Real)</option>
              <option value="both">Both</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Trade Type
            </label>
            <select
              value={settings.trade_type}
              onChange={(e) => setSettings({ ...settings, trade_type: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="market">Market</option>
              <option value="limit">Limit</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Position Size (USDT)
            </label>
            <input
              type="number"
              value={settings.position_size_usdt}
              onChange={(e) => setSettings({ ...settings, position_size_usdt: parseFloat(e.target.value) || 100 })}
              min="10"
              step="10"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Max Positions
            </label>
            <input
              type="number"
              value={settings.max_positions}
              onChange={(e) => setSettings({ ...settings, max_positions: parseInt(e.target.value) || 5 })}
              min="1"
              max="50"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Max Positions (Sandbox)
            </label>
            <input
              type="number"
              value={settings.max_positions_sandbox}
              onChange={(e) => setSettings({ ...settings, max_positions_sandbox: parseInt(e.target.value) || 5 })}
              min="1"
              max="50"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Max Positions (Live)
            </label>
            <input
              type="number"
              value={settings.max_positions_live}
              onChange={(e) => setSettings({ ...settings, max_positions_live: parseInt(e.target.value) || 3 })}
              min="1"
              max="20"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Leverage
            </label>
            <input
              type="number"
              value={settings.leverage}
              onChange={(e) => setSettings({ ...settings, leverage: parseInt(e.target.value) || 10 })}
              min="1"
              max="125"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Margin Mode
            </label>
            <select
              value={settings.margin_mode}
              onChange={(e) => setSettings({ ...settings, margin_mode: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            >
              <option value="crossed">Cross</option>
              <option value="isolated">Isolated</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Sniper Offset (%)
            </label>
            <input
              type="number"
              value={settings.sniper_offset_pct}
              onChange={(e) => setSettings({ ...settings, sniper_offset_pct: parseFloat(e.target.value) || 0.5 })}
              min="0"
              max="10"
              step="0.1"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Min Confidence
            </label>
            <input
              type="number"
              value={settings.min_confidence}
              onChange={(e) => setSettings({ ...settings, min_confidence: parseFloat(e.target.value) || 0.65 })}
              min="0"
              max="1"
              step="0.05"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Min Risk:Reward
            </label>
            <input
              type="number"
              value={settings.min_risk_reward}
              onChange={(e) => setSettings({ ...settings, min_risk_reward: parseFloat(e.target.value) || 1.5 })}
              min="0.1"
              step="0.1"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Pending TTL (min)
            </label>
            <input
              type="number"
              value={settings.pending_ttl_minutes}
              onChange={(e) => setSettings({ ...settings, pending_ttl_minutes: parseInt(e.target.value) || 30 })}
              min="1"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              TP Trail (%)
            </label>
            <input
              type="number"
              value={settings.tp_trail_pct}
              onChange={(e) => setSettings({ ...settings, tp_trail_pct: parseFloat(e.target.value) || 1.5 })}
              min="0"
              max="50"
              step="0.1"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
            />
          </div>

          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Execute Sandbox
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.execute_sandbox}
                onChange={(e) => setSettings({ ...settings, execute_sandbox: e.target.checked })}
                className="w-5 h-5 text-blue-600 border-gray-300 rounded"
              />
              <span className="text-sm text-gray-700 dark:text-gray-300">Execute in sandbox mode</span>
            </label>
          </div>

          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Execute Live
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.execute_live}
                onChange={(e) => setSettings({ ...settings, execute_live: e.target.checked })}
                className="w-5 h-5 text-red-600 border-gray-300 rounded"
              />
              <span className="text-sm text-gray-700 dark:text-gray-300">Execute live trades (REAL MONEY)</span>
            </label>
          </div>

          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Require AI Confirmation
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.require_ai_confirmation}
                onChange={(e) => setSettings({ ...settings, require_ai_confirmation: e.target.checked })}
                className="w-5 h-5 text-blue-600 border-gray-300 rounded"
              />
              <span className="text-sm text-gray-700 dark:text-gray-300">Require AI agent approval before executing</span>
            </label>
          </div>

          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Execute Immediately
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="checkbox"
                checked={settings.execute_immediately}
                onChange={(e) => setSettings({ ...settings, execute_immediately: e.target.checked })}
                className="w-5 h-5 text-blue-600 border-gray-300 rounded"
              />
              <span className="text-sm text-gray-700 dark:text-gray-300">Execute immediately on signal (no delay)</span>
            </label>
          </div>
        </div>

        <div className="mt-6 flex justify-end">
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-6 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Settings"}
          </button>
        </div>
      </div>

      {/* Recent Trades */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Recent Sniper Trades</h3>
          <button
            onClick={fetchTrades}
            disabled={loading}
            className="px-3 py-1 text-sm bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50"
          >
            {loading ? "⟳" : "🔄 Refresh"}
          </button>
        </div>

        {loading ? (
          <div className="p-8 text-center text-gray-500">Loading trades...</div>
        ) : trades.length === 0 ? (
          <div className="p-8 text-center text-gray-500">
            <p className="text-xl mb-2">📭 No trades yet</p>
            <p>Run the sniper or wait for signals to trigger auto-trades</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-500 dark:text-gray-400 text-sm border-b border-gray-200 dark:border-gray-700">
                  <th className="pb-3 px-4">Trade</th>
                  <th className="pb-3 px-4">Symbol</th>
                  <th className="pb-3 px-4">Side</th>
                  <th className="pb-3 px-4">Entry</th>
                  <th className="pb-3 px-4">SL / TP</th>
                  <th className="pb-3 px-4">Mode</th>
                  <th className="pb-3 px-4">Status</th>
                  <th className="pb-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((trade) => (
                  <tr key={trade.id} className="border-b border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50">
                    <td className="py-3 px-4 text-sm font-mono text-gray-600 dark:text-gray-400">#{trade.id}</td>
                    <td className="py-3 px-4 font-mono text-gray-900 dark:text-white">{trade.symbol}</td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                        trade.side === "buy" ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" :
                        "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                      }`}>
                        {trade.side.toUpperCase()}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm font-mono">{trade.entry_price?.toLocaleString() || "Market"}</td>
                    <td className="py-3 px-4 text-sm">
                      <div className="text-red-600 dark:text-red-400">SL: {trade.stop_loss?.toLocaleString() || "—"}</div>
                      <div className="text-green-600 dark:text-green-400">TP: {trade.take_profit?.toLocaleString() || "—"}</div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`px-2 py-1 text-xs rounded-full ${
                        trade.mode === "sandbox" ? "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" :
                        "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                      }`}>
                        {trade.mode}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                        trade.status === "filled" ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" :
                        trade.status === "placed" ? "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" :
                        trade.status === "pending" ? "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200" :
                        "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                      }`}>
                        {trade.status}
                      </span>
                    </td>
                    <td className="py-3 px-4">
                      {trade.status === "pending" && (
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleExecuteTrade(trade.id, "sandbox")}
                            className="px-2 py-1 text-xs bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200 rounded hover:bg-blue-200 dark:hover:bg-blue-800"
                          >
                            Sandbox
                          </button>
                          <button
                            onClick={() => handleExecuteTrade(trade.id, "live")}
                            className="px-2 py-1 text-xs bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200 rounded hover:bg-red-200 dark:hover:bg-red-800"
                          >
                            Live
                          </button>
                        </div>
                      )}
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