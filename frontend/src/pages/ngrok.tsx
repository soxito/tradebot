import { useState, useEffect, useCallback } from 'react';
import { apiClient } from '@/services/api';
import {
  Globe,
  Play,
  Square,
  RefreshCw,
  Settings2,
  Copy,
  CheckCircle,
  XCircle,
  AlertCircle,
  Loader2,
  ExternalLink,
  ShieldCheck,
} from 'lucide-react';

// ── Types ──────────────────────────────────────────────────────────────────────

interface TunnelInfo {
  name: string;
  local_addr: string;
  public_url: string;
  started_at: string;
}

interface NgrokStatus {
  state: 'stopped' | 'starting' | 'running' | 'error';
  error: string | null;
  tunnels: TunnelInfo[];
  oauth_provider: string;
  oauth_enforced: boolean;
  config?: NgrokConfig;
}

interface NgrokConfig {
  authtoken: string;
  backend_addr: string;
  frontend_addr: string;
  enable_on_start: boolean;
  oauth_provider: string;
  oauth_enforced: boolean;
  sources: Record<string, string>;
}

// ── Helpers ────────────────────────────────────────────────────────────────────

const STATE_COLOR: Record<string, string> = {
  running: 'text-emerald-400',
  starting: 'text-yellow-400',
  stopped: 'text-gray-400',
  error: 'text-red-400',
};

const STATE_ICON: Record<string, React.ReactNode> = {
  running: <CheckCircle className="w-4 h-4" />,
  starting: <Loader2 className="w-4 h-4 animate-spin" />,
  stopped: <XCircle className="w-4 h-4" />,
  error: <AlertCircle className="w-4 h-4" />,
};

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const handle = () => {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };
  return (
    <button onClick={handle} className="ml-2 text-gray-400 hover:text-white transition" title="Copy">
      {copied ? <CheckCircle className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
    </button>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default function NgrokPage() {
  const [status, setStatus] = useState<NgrokStatus | null>(null);
  const [config, setConfig] = useState<NgrokConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showConfig, setShowConfig] = useState(false);

  // Config form state
  const [cfgAuthtoken, setCfgAuthtoken] = useState('');
  const [cfgBackend, setCfgBackend] = useState('');
  const [cfgFrontend, setCfgFrontend] = useState('');
  const [cfgAutoStart, setCfgAutoStart] = useState(false);
  const [configSaving, setConfigSaving] = useState(false);
  const [configSaved, setConfigSaved] = useState(false);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await apiClient.ngrok.getStatus();
      setStatus(res.data);
      if (res.data.config) setConfig(res.data.config);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to fetch ngrok status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(() => {
      if (status?.state === 'starting') fetchStatus();
    }, 3000);
    return () => clearInterval(id);
  }, [fetchStatus, status?.state]);

  // Populate config form when config is loaded
  useEffect(() => {
    if (!config) return;
    setCfgBackend(config.backend_addr || '');
    setCfgFrontend(config.frontend_addr || '');
    setCfgAutoStart(config.enable_on_start || false);
    // Never pre-fill authtoken in UI for security
  }, [config]);

  const handleStart = async () => {
    setActionLoading(true);
    setError(null);
    try {
      await apiClient.ngrok.start();
      await fetchStatus();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to start ngrok');
    } finally {
      setActionLoading(false);
    }
  };

  const handleStop = async () => {
    setActionLoading(true);
    setError(null);
    try {
      await apiClient.ngrok.stop();
      await fetchStatus();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to stop ngrok');
    } finally {
      setActionLoading(false);
    }
  };

  const handleRestart = async () => {
    setActionLoading(true);
    setError(null);
    try {
      await apiClient.ngrok.restart();
      await fetchStatus();
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to restart ngrok');
    } finally {
      setActionLoading(false);
    }
  };

  const handleSaveConfig = async () => {
    setConfigSaving(true);
    setError(null);
    try {
      const payload: Record<string, any> = {
        backend_addr_override: cfgBackend || null,
        frontend_addr_override: cfgFrontend || null,
        enable_on_start: cfgAutoStart,
      };
      if (cfgAuthtoken.trim()) {
        payload.authtoken_override = cfgAuthtoken.trim();
      }
      const res = await apiClient.ngrok.updateConfig(payload);
      setConfig(res.data);
      setConfigSaved(true);
      setCfgAuthtoken('');
      setTimeout(() => setConfigSaved(false), 2500);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to save config');
    } finally {
      setConfigSaving(false);
    }
  };

  const isRunning = status?.state === 'running';
  const isStopped = status?.state === 'stopped' || status?.state === 'error';

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Globe className="w-6 h-6 text-tradebot-accent" />
          <div>
            <h1 className="text-xl font-bold text-white">Ngrok Tunnels</h1>
            <p className="text-sm text-gray-400">Expose backend and frontend via public HTTPS URLs</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchStatus}
            disabled={loading}
            className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 transition"
            title="Refresh"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={() => setShowConfig(!showConfig)}
            className={`p-2 rounded-lg transition ${showConfig ? 'bg-tradebot-accent/20 text-tradebot-accent' : 'text-gray-400 hover:text-white hover:bg-gray-800'}`}
            title="Configure"
          >
            <Settings2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 flex items-center gap-3">
          <AlertCircle className="w-4 h-4 text-red-400 shrink-0" />
          <span className="text-sm text-red-300">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto text-red-400 hover:text-red-200 text-xs">Dismiss</button>
        </div>
      )}

      {/* Status Card */}
      <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-5 space-y-4">
        {/* State row */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className={`flex items-center gap-1.5 font-semibold capitalize ${STATE_COLOR[status?.state ?? 'stopped']}`}>
              {STATE_ICON[status?.state ?? 'stopped']}
              {status?.state ?? 'stopped'}
            </span>
            {status?.state === 'running' && (
              <span className="text-xs text-gray-500">• {status.tunnels.length} tunnel{status.tunnels.length !== 1 ? 's' : ''} active</span>
            )}
          </div>

          {/* OAuth badge */}
          <div className="flex items-center gap-1.5 text-xs text-emerald-400 bg-emerald-400/10 px-2.5 py-1 rounded-full">
            <ShieldCheck className="w-3 h-3" />
            Google OAuth enforced
          </div>
        </div>

        {/* Error detail */}
        {status?.error && (
          <div className="text-xs text-red-400 bg-red-500/10 rounded-lg px-3 py-2">
            {status.error}
          </div>
        )}

        {/* Tunnel cards */}
        {status?.tunnels && status.tunnels.length > 0 && (
          <div className="space-y-2">
            {status.tunnels.map((t) => (
              <div key={t.name} className="bg-gray-800/50 rounded-lg px-4 py-3 flex items-center gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-xs font-semibold uppercase tracking-wide text-tradebot-accent">
                      {t.name}
                    </span>
                    <span className="text-xs text-gray-500">→</span>
                    <span className="text-xs text-gray-400">{t.local_addr}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <a
                      href={t.public_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm text-white font-mono truncate hover:text-tradebot-accent transition"
                    >
                      {t.public_url}
                    </a>
                    <CopyButton value={t.public_url} />
                    <a href={t.public_url} target="_blank" rel="noopener noreferrer" className="text-gray-500 hover:text-white transition">
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Control buttons */}
        <div className="flex gap-2 pt-1">
          {isStopped && (
            <button
              onClick={handleStart}
              disabled={actionLoading}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium transition disabled:opacity-50"
            >
              {actionLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              Start Tunnels
            </button>
          )}
          {isRunning && (
            <>
              <button
                onClick={handleStop}
                disabled={actionLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-red-700 hover:bg-red-600 text-white text-sm font-medium transition disabled:opacity-50"
              >
                {actionLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
                Stop
              </button>
              <button
                onClick={handleRestart}
                disabled={actionLoading}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-700 hover:bg-gray-600 text-white text-sm font-medium transition disabled:opacity-50"
              >
                <RefreshCw className="w-4 h-4" />
                Restart
              </button>
            </>
          )}
        </div>
      </div>

      {/* Config Panel */}
      {showConfig && (
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-5 space-y-4">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Settings2 className="w-4 h-4 text-tradebot-accent" />
            Configuration
            {config && (
              <span className="ml-auto text-xs text-gray-500 font-normal">
                authtoken: <span className={config.sources.authtoken === 'db' ? 'text-yellow-400' : 'text-gray-400'}>{config.sources.authtoken}</span>
                {' · '}backend: <span className={config.sources.backend_addr === 'db' ? 'text-yellow-400' : 'text-gray-400'}>{config.sources.backend_addr}</span>
                {' · '}frontend: <span className={config.sources.frontend_addr === 'db' ? 'text-yellow-400' : 'text-gray-400'}>{config.sources.frontend_addr}</span>
              </span>
            )}
          </h2>

          <div className="grid gap-3">
            <div>
              <label className="block text-xs text-gray-400 mb-1">
                Auth Token <span className="text-gray-500">(leave blank to keep existing / use env NGROK_AUTHTOKEN)</span>
              </label>
              <input
                type="password"
                value={cfgAuthtoken}
                onChange={(e) => setCfgAuthtoken(e.target.value)}
                placeholder="••••••••••••••••••••"
                className="w-full bg-gray-800 text-white text-sm rounded-lg px-3 py-2 border border-gray-700 focus:border-tradebot-accent focus:outline-none"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Backend Address</label>
                <input
                  type="text"
                  value={cfgBackend}
                  onChange={(e) => setCfgBackend(e.target.value)}
                  placeholder="http://localhost:1448"
                  className="w-full bg-gray-800 text-white text-sm rounded-lg px-3 py-2 border border-gray-700 focus:border-tradebot-accent focus:outline-none"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Frontend Address</label>
                <input
                  type="text"
                  value={cfgFrontend}
                  onChange={(e) => setCfgFrontend(e.target.value)}
                  placeholder="http://localhost:3000"
                  className="w-full bg-gray-800 text-white text-sm rounded-lg px-3 py-2 border border-gray-700 focus:border-tradebot-accent focus:outline-none"
                />
              </div>
            </div>
            <div className="flex items-center gap-3">
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={cfgAutoStart}
                  onChange={(e) => setCfgAutoStart(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-gray-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-tradebot-accent"></div>
                <span className="ml-2 text-sm text-gray-300">Auto-start on backend launch</span>
              </label>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={handleSaveConfig}
              disabled={configSaving}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-tradebot-accent hover:bg-tradebot-accent/80 text-white text-sm font-medium transition disabled:opacity-50"
            >
              {configSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              Save Config
            </button>
            {configSaved && (
              <span className="flex items-center gap-1.5 text-sm text-emerald-400">
                <CheckCircle className="w-4 h-4" /> Saved
              </span>
            )}
          </div>

          {/* OAuth notice */}
          <p className="text-xs text-gray-500 flex items-center gap-1.5">
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
            Google OAuth is always enforced on all tunnels and cannot be disabled.
          </p>
        </div>
      )}
    </div>
  );
}
