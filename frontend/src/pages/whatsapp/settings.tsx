"use client";

import { useEffect, useState } from "react";
import { useTranslation } from "next-i18next";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:1448/api/v1";

export default function WhatsAppSettings({ onSessionChange }) {
  const { t } = useTranslation("common");
  const [config, setConfig] = useState({
    openwa_base_url: "http://localhost:2785",
    openwa_api_key: "",
    webhook_secret: "",
    default_session_name: "tradebot_whatsapp",
    poll_interval_seconds: 300,
  });
  const [session, setSession] = useState(null);
  const [qrCode, setQrCode] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState({ type: "", text: "" });

  useEffect(() => {
    fetchConfig();
    fetchSession();
  }, []);

  const fetchConfig = async () => {
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/settings`);
      const data = await res.json();
      if (data.openwa_base_url) {
        setConfig({
          openwa_base_url: data.openwa_base_url,
          openwa_api_key: "",
          webhook_secret: "",
          default_session_name: data.default_session_name,
          poll_interval_seconds: data.poll_interval_seconds,
        });
      }
    } catch (e) {
      console.error("Failed to fetch config:", e);
    }
  };

  const fetchSession = async () => {
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/session/default/status`);
      const data = await res.json();
      setSession(data);
      if (onSessionChange) onSessionChange(data);
    } catch (e) {
      console.error("Failed to fetch session:", e);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setMessage({ type: "", text: "" });
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      const data = await res.json();
      if (res.ok) {
        setMessage({ type: "success", text: "Settings saved successfully!" });
        fetchSession();
      } else {
        setMessage({ type: "error", text: data.detail || "Failed to save settings" });
      }
    } catch (e) {
      setMessage({ type: "error", text: "Failed to save settings" });
    } finally {
      setSaving(false);
    }
  };

  const handleCreateSession = async () => {
    setLoading(true);
    setMessage({ type: "", text: "" });
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/session/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: config.default_session_name }),
      });
      const data = await res.json();
      if (res.ok) {
        setMessage({ type: "success", text: "Session created! Fetching QR code..." });
        setTimeout(fetchQrCode, 1000);
      } else {
        setMessage({ type: "error", text: data.detail || "Failed to create session" });
      }
    } catch (e) {
      setMessage({ type: "error", text: "Failed to create session" });
    } finally {
      setLoading(false);
    }
  };

  const fetchQrCode = async () => {
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/session/default/qr`);
      const data = await res.json();
      if (data.qr_code) {
        setQrCode(data.qr_code);
        setMessage({ type: "success", text: "QR code generated! Scan with WhatsApp." });
      }
      fetchSession();
    } catch (e) {
      setMessage({ type: "error", text: "Failed to fetch QR code" });
    }
  };

  const handleTestConnection = async () => {
    setLoading(true);
    setMessage({ type: "", text: "" });
    try {
      const res = await fetch(`${API_URL}/plugins/whatsapp/test-connection`, { method: "POST" });
      const data = await res.json();
      if (data.any_ok) {
        setMessage({ type: "success", text: "Connection test passed!" });
      } else {
        setMessage({ type: "error", text: "Connection test failed" });
      }
      fetchSession();
    } catch (e) {
      setMessage({ type: "error", text: "Connection test failed" });
    } finally {
      setLoading(false);
    }
  };

  const handleStopSession = async () => {
    try {
      await fetch(`${API_URL}/plugins/whatsapp/session/default/stop`, { method: "POST" });
      fetchSession();
    } catch (e) {
      console.error("Failed to stop session:", e);
    }
  };

  return (
    <div className="space-y-6">
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-6">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-6">
          OpenWA Gateway Configuration
        </h2>

        {message.text && (
          <div
            className={`mb-4 p-4 rounded-lg ${
              message.type === "success"
                ? "bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200"
                : "bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200"
            }`}
          >
            {message.text}
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                OpenWA Gateway URL
              </label>
              <input
                type="text"
                value={config.openwa_base_url}
                onChange={(e) => setConfig({ ...config, openwa_base_url: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                placeholder="http://localhost:2785"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Base URL of your self-hosted OpenWA Gateway
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                OpenWA API Key
              </label>
              <input
                type="password"
                value={config.openwa_api_key}
                onChange={(e) => setConfig({ ...config, openwa_api_key: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                placeholder="Enter API key from OpenWA dashboard"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Leave empty to keep current key
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Webhook Secret
              </label>
              <input
                type="password"
                value={config.webhook_secret}
                onChange={(e) => setConfig({ ...config, webhook_secret: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                placeholder="HMAC secret for webhook verification"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Used to verify incoming webhooks from OpenWA Gateway
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Default Session Name
              </label>
              <input
                type="text"
                value={config.default_session_name}
                onChange={(e) => setConfig({ ...config, default_session_name: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Poll Interval (seconds)
              </label>
              <input
                type="number"
                value={config.poll_interval_seconds}
                onChange={(e) => setConfig({ ...config, poll_interval_seconds: parseInt(e.target.value) || 300 })}
                min={30}
                max={3600}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                How often to poll for new messages (fallback when webhooks unavailable)
              </p>
            </div>
          </div>

          <div className="space-y-4">
            <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4">
              <h3 className="font-medium text-gray-900 dark:text-white mb-3">
                Webhook Configuration
              </h3>
              <p className="text-sm text-gray-600 dark:text-gray-400 mb-3">
                Configure your OpenWA Gateway to send webhooks to:
              </p>
              <code className="block bg-gray-100 dark:bg-gray-800 p-3 rounded text-sm text-blue-600 dark:text-blue-400">
                {typeof window !== "undefined" ? window.location.origin : "https://your-domain.com"}
                /api/plugins/whatsapp/webhook/{{session_id}}
              </code>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                Set this in OpenWA Dashboard → Sessions → Webhooks, or via API.
              </p>
            </div>

            <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4">
              <h3 className="font-medium text-gray-900 dark:text-white mb-3">
                Connection Test
              </h3>
              <div className="flex gap-3">
                <button
                  onClick={handleTestConnection}
                  disabled={loading || saving}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                >
                  {loading ? "Testing..." : "Test Connection"}
                </button>
              </div>
            </div>

            <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-4">
              <h3 className="font-medium text-gray-900 dark:text-white mb-3">
                WhatsApp Session Management
              </h3>
              <div className="flex flex-wrap gap-3 mb-4">
                <button
                  onClick={handleCreateSession}
                  disabled={loading || saving}
                  className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
                >
                  {loading ? "Creating..." : "Create & Start Session"}
                </button>
                {session?.db?.status === "qr_ready" && (
                  <button
                    onClick={fetchQrCode}
                    disabled={loading}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                  >
                    Refresh QR Code
                  </button>
                )}
                {session?.db?.status === "ready" && (
                  <button
                    onClick={handleStopSession}
                    className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
                  >
                    Stop Session
                  </button>
                )}
              </div>

              {session && (
                <div className="bg-white dark:bg-gray-800 rounded-lg p-4 border border-gray-200 dark:border-gray-700">
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Session ID</p>
                      <p className="font-mono text-sm">{session.db.session_id || "default"}</p>
                    </div>
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Status</p>
                      <p className={`font-medium capitalize ${
                        session.db.status === "ready"
                          ? "text-green-600 dark:text-green-400"
                          : session.db.status === "qr_ready"
                          ? "text-blue-600 dark:text-blue-400"
                          : "text-yellow-600 dark:text-yellow-400"
                      }`}>
                        {session.db.status}
                      </p>
                    </div>
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Phone</p>
                      <p className="font-mono text-sm">{session.db.phone || "—"}</p>
                    </div>
                    <div>
                      <p className="text-sm text-gray-500 dark:text-gray-400">Name</p>
                      <p className="font-mono text-sm">{session.db.platform || session.db.name || "—"}</p>
                    </div>
                  </div>
                </div>
              )}

              {qrCode && (
                <div className="mt-4 p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 text-center">
                  <h4 className="font-medium text-gray-900 dark:text-white mb-3">
                    Scan QR Code with WhatsApp
                  </h4>
                  <img
                    src={`data:image/png;base64,${qrCode}`}
                    alt="WhatsApp QR Code"
                    className="mx-auto max-w-xs"
                  />
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                    Open WhatsApp → Settings → Linked Devices → Link a Device
                  </p>
                </div>
              )}
            </div>
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
    </div>
  );
}