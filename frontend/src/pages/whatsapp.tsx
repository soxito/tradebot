"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/services/api";
import Link from "next/link";
import dynamic from "next/dynamic";

import Layout from "@/components/Layout";
import ConnectionStatus from "@/components/ConnectionStatus";

// Resolved lazily — the desktop build picks the API port at launch.
const API_URL = () => getApiBaseUrl();

// These live in `pages/whatsapp/`, not alongside this file. Relative to
// `pages/whatsapp.tsx`, "./settings" and "./signals" resolved to the app's main
// Settings and Signals pages — wrong component, no error — while "./channels"
// and "./sniper" had no top-level sibling and failed the production build.
const WhatsAppSettings = dynamic(() => import("./whatsapp/settings"), { ssr: false });
const WhatsAppChannels = dynamic(() => import("./whatsapp/channels"), { ssr: false });
const WhatsAppSignals = dynamic(() => import("./whatsapp/signals"), { ssr: false });
const WhatsAppSniper = dynamic(() => import("./whatsapp/sniper"), { ssr: false });

const Tabs = [
  { id: "dashboard", label: "Dashboard", icon: "📱" },
  { id: "settings", label: "Settings", icon: "⚙️" },
  { id: "channels", label: "Channels", icon: "📢" },
  { id: "signals", label: "Signals", icon: "📊" },
  { id: "sniper", label: "Sniper", icon: "🎯" },
];

export default function WhatsAppPage() {
  const [activeTab, setActiveTab] = useState("dashboard");
  const [connected, setConnected] = useState(false);
  const [session, setSession] = useState(null);
  const [stats, setStats] = useState({
    total_channels: 0,
    active_signals: 0,
    sniper_trades: 0,
    last_poll: null,
  });

  useEffect(() => {
    checkConnection();
    fetchStats();
    fetchSession();
  }, []);

  const checkConnection = async () => {
    try {
      const res = await fetch(`${API_URL()}/plugins/whatsapp/test-connection`);
      const data = await res.json();
      setConnected(data.any_ok || false);
    } catch {
      setConnected(false);
    }
  };

  const fetchStats = async () => {
    try {
      const [channelsRes, signalsRes, tradesRes] = await Promise.all([
        fetch(`${API_URL()}/plugins/whatsapp/channels`),
        fetch(`${API_URL()}/plugins/whatsapp/signals?status=active&limit=1`),
        fetch(`${API_URL()}/plugins/whatsapp/sniper/trades?status=placed,filled&limit=1`),
      ]);
      const channels = await channelsRes.json();
      const signals = await signalsRes.json();
      const trades = await tradesRes.json();

      setStats({
        total_channels: channels.length,
        active_signals: signals.length,
        sniper_trades: trades.length,
        last_poll: new Date().toISOString(),
      });
    } catch (e) {
      console.error("Failed to fetch stats:", e);
    }
  };

  const fetchSession = async () => {
    try {
      const res = await fetch(`${API_URL()}/plugins/whatsapp/session/default/status`);
      const data = await res.json();
      setSession(data);
    } catch {
      setSession(null);
    }
  };

  return (
    <Layout>
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
        <ConnectionStatus />
        
        {/* Header */}
        <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-40">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex items-center justify-between h-16">
              <div className="flex items-center gap-4">
                <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
                  📱 WhatsApp Signal Bot
                </h1>
                <span className={`px-2 py-1 text-xs rounded-full ${
                  connected ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                    : "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"
                }`}>
                  {connected ? "Connected" : "Disconnected"}
                </span>
              </div>
              
              {/* Session Status */}
              {session && session.db && (
                <div className="flex items-center gap-4 text-sm">
                  <span className="text-gray-500 dark:text-gray-400">
                    Session: {session.db.name || "default"}
                  </span>
                  <span className={`px-2 py-1 rounded ${
                    session.db.status === "ready" || session.db.status === "authenticated"
                      ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                      : "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200"
                  }`}>
                    {session.db.status}
                  </span>
                  {session.db.qr_code && (
                    <Link
                      href="/whatsapp?tab=settings"
                      className="text-blue-600 hover:text-blue-800 dark:text-blue-400 text-sm"
                    >
                      Scan QR
                    </Link>
                  )}
                </div>
              )}
            </div>
          </div>
        </header>

        {/* Tab Navigation */}
        <nav className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 sticky top-16 z-30">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex overflow-x-auto">
              {Tabs.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
                    activeTab === tab.id
                      ? "border-blue-500 text-blue-600 dark:text-blue-400"
                      : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 dark:text-gray-400 dark:hover:text-gray-200"
                  }`}
                >
                  <span className="text-lg">{tab.icon}</span>
                  {tab.label}
                </button>
              ))}
            </div>
          </div>
        </nav>

        {/* Tab Content */}
        <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
          {activeTab === "dashboard" && (
            <div className="space-y-6 animate-fade-in">
              {/* Stats Grid */}
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                <StatCard
                  title="Monitored Channels"
                  value={stats.total_channels}
                  icon="📢"
                  color="blue"
                />
                <StatCard
                  title="Active Signals"
                  value={stats.active_signals}
                  icon="📊"
                  color="green"
                />
                <StatCard
                  title="Sniper Trades"
                  value={stats.sniper_trades}
                  icon="🎯"
                  color="purple"
                />
                <StatCard
                  title="Last Poll"
                  value={stats.last_poll ? new Date(stats.last_poll).toLocaleTimeString() : "Never"}
                  icon="🕐"
                  color="gray"
                />
              </div>

              {/* Quick Actions */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <Link
                  href="/whatsapp?tab=settings"
                  className="bg-white dark:bg-gray-800 rounded-xl p-6 shadow-sm border border-gray-200 dark:border-gray-700 hover:border-blue-300 dark:hover:border-blue-700 transition-colors"
                >
                  <div className="flex items-center gap-4">
                    <div className="p-3 bg-blue-100 dark:bg-blue-900 rounded-lg">
                      <span className="text-2xl">⚙️</span>
                    </div>
                    <div>
                      <h3 className="font-semibold text-gray-900 dark:text-white">Settings</h3>
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        Configure OpenWA Gateway, API keys, and webhook URL
                      </p>
                    </div>
                  </div>
                </Link>

                <Link
                  href="/whatsapp?tab=channels"
                  className="bg-white dark:bg-gray-800 rounded-xl p-6 shadow-sm border border-gray-200 dark:border-gray-700 hover:border-green-300 dark:hover:border-green-700 transition-colors"
                >
                  <div className="flex items-center gap-4">
                    <div className="p-3 bg-green-100 dark:bg-green-900 rounded-lg">
                      <span className="text-2xl">📢</span>
                    </div>
                    <div>
                      <h3 className="font-semibold text-gray-900 dark:text-white">Channels</h3>
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        Add and manage WhatsApp groups/channels to monitor
                      </p>
                    </div>
                  </div>
                </Link>

                <Link
                  href="/whatsapp?tab=sniper"
                  className="bg-white dark:bg-gray-800 rounded-xl p-6 shadow-sm border border-gray-200 dark:border-gray-700 hover:border-purple-300 dark:hover:border-purple-700 transition-colors"
                >
                  <div className="flex items-center gap-4">
                    <div className="p-3 bg-purple-100 dark:bg-purple-900 rounded-lg">
                      <span className="text-2xl">🎯</span>
                    </div>
                    <div>
                      <h3 className="font-semibold text-gray-900 dark:text-white">Sniper Auto-Trade</h3>
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        Configure auto-trading from WhatsApp signals
                      </p>
                    </div>
                  </div>
                </Link>
              </div>

              {/* Session Quick Setup */}
              {!session?.db?.authenticated && (
                <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-xl p-6">
                  <div className="flex items-start gap-4">
                    <div className="p-2 bg-yellow-100 dark:bg-yellow-900 rounded-lg">
                      <span className="text-2xl">⚠️</span>
                    </div>
                    <div className="flex-1">
                      <h3 className="font-semibold text-yellow-800 dark:text-yellow-200">
                        WhatsApp Not Connected
                      </h3>
                      <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                        Go to Settings to create a session and scan the QR code to connect your WhatsApp account.
                      </p>
                      <Link
                        href="/whatsapp?tab=settings"
                        className="inline-block mt-3 px-4 py-2 bg-yellow-600 text-white rounded-lg hover:bg-yellow-700 text-sm font-medium"
                      >
                        Connect WhatsApp
                      </Link>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          {activeTab === "settings" && <WhatsAppSettings onSessionChange={fetchSession} />}
          {activeTab === "channels" && <WhatsAppChannels onStatsChange={fetchStats} />}
          {activeTab === "signals" && <WhatsAppSignals onStatsChange={fetchStats} />}
          {activeTab === "sniper" && <WhatsAppSniper onStatsChange={fetchStats} />}
        </main>
      </div>
    </Layout>
  );
}

function StatCard({ title, value, icon, color }) {
  const colors = {
    blue: "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800 text-blue-600 dark:text-blue-400",
    green: "bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800 text-green-600 dark:text-green-400",
    purple: "bg-purple-50 dark:bg-purple-900/20 border-purple-200 dark:border-purple-800 text-purple-600 dark:text-purple-400",
    gray: "bg-gray-50 dark:bg-gray-800 border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400",
  };

  return (
    <div className={`bg-white dark:bg-gray-800 rounded-xl p-5 shadow-sm border ${colors[color] || colors.gray}`}>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-gray-500 dark:text-gray-400">{title}</p>
          <p className="text-2xl font-bold mt-1">{value}</p>
        </div>
        <span className="text-3xl">{icon}</span>
      </div>
    </div>
  );
}