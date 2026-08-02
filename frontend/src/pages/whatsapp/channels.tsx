"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/services/api";
import Link from "next/link";

// Resolved lazily — the desktop build picks the API port at launch.
const API_URL = () => getApiBaseUrl();

export default function WhatsAppChannels({ onStatsChange }) {
  const [channels, setChannels] = useState([]);
  const [chats, setChats] = useState([]);
  const [loading, setLoading] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({
    name: "",
    kind: "signals",
    source_type: "group",
    chat_id: "",
    session_id: "",
    is_active: true,
    parse_signals: true,
  });
  const [message, setMessage] = useState({ type: "", text: "" });

  useEffect(() => {
    fetchChannels();
    if (onStatsChange) onStatsChange();
  }, []);

  const fetchChannels = async () => {
    try {
      const res = await fetch(`${API_URL()}/plugins/whatsapp/channels`);
      const data = await res.json();
      setChannels(data);
    } catch (e) {
      console.error("Failed to fetch channels:", e);
    }
  };

  const fetchChats = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL()}/plugins/whatsapp/chats`);
      const data = await res.json();
      setChats(data);
    } catch (e) {
      console.error("Failed to fetch chats:", e);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setMessage({ type: "", text: "" });
    try {
      const url = editing
        ? `${API_URL()}/plugins/whatsapp/channels/${editing.id}`
        : `${API_URL()}/plugins/whatsapp/channels`;
      const method = editing ? "PATCH" : "POST";
      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const data = await res.json();
      if (res.ok) {
        setMessage({ type: "success", text: editing ? "Channel updated!" : "Channel added!" });
        setShowModal(false);
        setEditing(null);
        fetchChannels();
      } else {
        setMessage({ type: "error", text: data.detail || "Failed to save channel" });
      }
    } catch (e) {
      setMessage({ type: "error", text: "Failed to save channel" });
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm("Delete this channel?")) return;
    try {
      await fetch(`${API_URL()}/plugins/whatsapp/channels/${id}`, { method: "DELETE" });
      fetchChannels();
    } catch (e) {
      console.error("Failed to delete:", e);
    }
  };

  const openCreate = () => {
    setEditing(null);
    setForm({
      name: "",
      kind: "signals",
      source_type: "group",
      chat_id: "",
      session_id: "",
      is_active: true,
      parse_signals: true,
    });
    setShowModal(true);
  };

  const openEdit = (channel) => {
    setEditing(channel);
    setForm({
      name: channel.name,
      kind: channel.kind,
      source_type: channel.source_type,
      chat_id: channel.chat_id,
      session_id: channel.session_id,
      is_active: channel.is_active,
      parse_signals: channel.parse_signals,
    });
    setShowModal(true);
  };

  const handlePreview = async (channelId) => {
    setLoading(true);
    try {
      const res = await fetch(`${API_URL()}/plugins/whatsapp/channels/${channelId}/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 20 }),
      });
      const data = await res.json();
      alert(JSON.stringify(data, null, 2));
    } catch (e) {
      console.error("Preview failed:", e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-gray-900 dark:text-white">Monitored Channels</h2>
          <p className="text-gray-600 dark:text-gray-400">WhatsApp groups and contacts to monitor for signals</p>
        </div>
        <button
          onClick={openCreate}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 flex items-center gap-2"
        >
          <span>➕</span> Add Channel
        </button>
      </div>

      {message.text && (
        <div
          className={`p-4 rounded-lg ${
            message.type === "success"
              ? "bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200"
              : "bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200"
          }`}
        >
          {message.text}
        </div>
      )}

      {/* Discovery Section */}
      <div className="bg-white dark:bg-gray-800 rounded-xl p-6 shadow-sm border border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Discover Chats</h3>
          <button
            onClick={fetchChats}
            disabled={loading}
            className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50 flex items-center gap-2"
          >
            {loading ? "🔄 Loading..." : "🔍 Discover Chats"}
          </button>
        </div>

        {chats.length > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 max-h-96 overflow-y-auto">
            {chats.map((chat) => (
              <div
                key={chat.id}
                className="p-4 bg-gray-50 dark:bg-gray-700/50 rounded-lg border border-gray-200 dark:border-gray-600 hover:border-blue-300 dark:hover:border-blue-700 cursor-pointer transition-colors"
                onClick={() => {
                  setForm({ ...form, name: chat.name, chat_id: chat.id, source_type: chat.type });
                  openCreate();
                }}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className="text-2xl">{chat.type === "group" ? "👥" : chat.type === "contact" ? "👤" : "📢"}</span>
                    <div>
                      <p className="font-medium text-gray-900 dark:text-white">{chat.name}</p>
                      <p className="text-xs text-gray-500 dark:text-gray-400">{chat.id}</p>
                    </div>
                  </div>
                  <span className={`px-2 py-1 text-xs rounded-full ${
                    chat.type === "group" ? "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" :
                    chat.type === "contact" ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" :
                    "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200"
                  }`}>
                    {chat.type}
                  </span>
                </div>
                {chat.participant_count && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{chat.participant_count} participants</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Channels Table */}
      <div className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        {channels.length === 0 ? (
          <div className="p-12 text-center">
            <div className="text-4xl mb-4">📭</div>
            <h3 className="text-lg font-medium text-gray-900 dark:text-white mb-2">No channels configured</h3>
            <p className="text-gray-500 dark:text-gray-400 mb-4">Click "Discover Chats" to find WhatsApp groups, then add them here</p>
            <button
              onClick={fetchChats}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
            >
              Discover Chats
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-left text-gray-500 dark:text-gray-400 text-sm border-b border-gray-200 dark:border-gray-700">
                  <th className="pb-3 px-4">Channel</th>
                  <th className="pb-3 px-4">Type</th>
                  <th className="pb-3 px-4">Session</th>
                  <th className="pb-3 px-4">Status</th>
                  <th className="pb-3 px-4">Last Message</th>
                  <th className="pb-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {channels.map((channel) => (
                  <tr key={channel.id} className="border-b border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-700/50">
                    <td className="py-3 px-4">
                      <div className="font-medium text-gray-900 dark:text-white">{channel.name}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 font-mono">{channel.chat_id}</div>
                    </td>
                    <td className="py-3 px-4">
                      <span className={`px-2 py-1 text-xs rounded-full ${
                        channel.kind === "signals" ? "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" :
                        channel.kind === "news" ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" :
                        "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200"
                      }`}>
                        {channel.kind}
                      </span>
                      <span className="ml-1 px-2 py-1 text-xs rounded-full bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">
                        {channel.source_type}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-600 dark:text-gray-400 font-mono">
                      {channel.session_id}
                    </td>
                    <td className="py-3 px-4">
                      <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${
                        channel.is_active
                          ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200"
                          : "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400"
                      }`}>
                        {channel.is_active ? "🟢 Active" : "⚪ Inactive"}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                      {channel.last_message_at
                        ? new Date(channel.last_message_at).toLocaleString()
                        : "Never"}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => handlePreview(channel.id)}
                          disabled={loading}
                          className="px-2 py-1 text-xs bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200 rounded hover:bg-blue-200 dark:hover:bg-blue-800"
                        >
                          Preview
                        </button>
                        <button
                          onClick={() => openEdit(channel)}
                          className="px-2 py-1 text-xs bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDelete(channel.id)}
                          className="px-2 py-1 text-xs bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200 rounded hover:bg-red-200 dark:hover:bg-red-800"
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-gray-800 rounded-xl max-w-md w-full max-h-[90vh] overflow-y-auto">
            <div className="p-6 border-b border-gray-200 dark:border-gray-700">
              <h3 className="text-xl font-semibold text-gray-900 dark:text-white">
                {editing ? "Edit Channel" : "Add New Channel"}
              </h3>
            </div>
            <form onSubmit={handleSubmit} className="p-6 space-y-4">
              {message.text && (
                <div className={`p-3 rounded-lg text-sm ${
                  message.type === "success" ? "bg-green-50 text-green-800 dark:bg-green-900/20 dark:text-green-200" :
                  "bg-red-50 text-red-800 dark:bg-red-900/20 dark:text-red-200"
                }`}>
                  {message.text}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Name *</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  required
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Kind *</label>
                  <select
                    value={form.kind}
                    onChange={(e) => setForm({ ...form, kind: e.target.value })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  >
                    <option value="signals">Signals</option>
                    <option value="news">News</option>
                    <option value="volume_alerts">Volume Alerts</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Source Type *</label>
                  <select
                    value={form.source_type}
                    onChange={(e) => setForm({ ...form, source_type: e.target.value })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                  >
                    <option value="group">Group</option>
                    <option value="contact">Contact</option>
                    <option value="broadcast">Broadcast</option>
                    <option value="community">Community</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Chat ID *</label>
                <input
                  type="text"
                  value={form.chat_id}
                  onChange={(e) => setForm({ ...form, chat_id: e.target.value })}
                  required
                  placeholder="e.g., 123456789@g.us"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">WhatsApp chat ID (e.g., 123456789@g.us for groups)</p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Session ID</label>
                <input
                  type="text"
                  value={form.session_id}
                  onChange={(e) => setForm({ ...form, session_id: e.target.value })}
                  placeholder="default"
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
                />
              </div>

              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.is_active}
                    onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                    className="w-4 h-4 text-blue-600 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">Active</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.parse_signals}
                    onChange={(e) => setForm({ ...form, parse_signals: e.target.checked })}
                    className="w-4 h-4 text-blue-600 border-gray-300 rounded"
                  />
                  <span className="text-sm text-gray-700 dark:text-gray-300">Parse Signals</span>
                </label>
              </div>

              <div className="flex justify-end gap-3 pt-4 border-t border-gray-200 dark:border-gray-700">
                <button
                  type="button"
                  onClick={() => setShowModal(false)}
                  className="px-4 py-2 text-gray-700 dark:text-gray-200 bg-gray-100 dark:bg-gray-700 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={loading}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                >
                  {loading ? "Saving..." : (editing ? "Update" : "Create")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}