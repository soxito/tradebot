import { useCallback, useState } from 'react';
import { apiClient } from '@/services/api';
import { useSmartPoll } from '@/hooks/useSmartPoll';
import {
  Activity,
  Cpu,
  MemoryStick,
  Gauge,
  Pause,
  Play,
  Square,
  RefreshCw,
  Lock,
  Zap,
  BatteryLow,
  Scale,
} from 'lucide-react';

// ── Types ───────────────────────────────────────────────────────────────────

interface HostSnapshot {
  available: boolean;
  cpu_percent?: number;
  cpu_count?: number;
  mem_percent?: number;
  mem_used?: number;
  mem_total?: number;
  swap_percent?: number;
}

interface LoopLag {
  available: boolean;
  p50_ms?: number;
  p95_ms?: number;
  max_ms?: number;
  samples?: number;
}

interface OffloadStats {
  in_flight?: number;
  heavy_in_flight?: number;
  heavy_queue_depth?: number;
  total_rejected?: number;
  total_timeouts?: number;
}

interface Resources {
  tier: string;
  profile: string | null;
  host: HostSnapshot;
  process: { available: boolean; rss?: number; cpu_percent?: number };
  loop_lag: LoopLag;
  offload: OffloadStats;
}

interface TaskInfo {
  id: string;
  name: string;
  category: string;
  critical: boolean;
  running: boolean | null;
  paused: boolean;
  paused_by: string | null;
  interval_seconds: number;
  cycle_count: number;
  error_count: number;
  last_error: string | null;
  last_run: string | null;
  cumulative_cpu_ms: number;
}

interface TasksSnapshot {
  tier: string;
  profile: string | null;
  task_count: number;
  paused_count: number;
  paused: { id: string; paused_by: string | null }[];
  tasks: TaskInfo[];
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function fmtBytes(n?: number): string {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${u[i]}`;
}

function Bar({ pct, color }: { pct: number; color: string }) {
  return (
    <div className="w-full h-2 bg-gray-700 rounded overflow-hidden">
      <div className={`h-full ${color}`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
    </div>
  );
}

function StatCard({ icon, label, value, children }: {
  icon: React.ReactNode; label: string; value: React.ReactNode; children?: React.ReactNode;
}) {
  return (
    <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-4">
      <div className="flex items-center gap-2 text-gray-400 text-xs uppercase tracking-wide">
        {icon}<span>{label}</span>
      </div>
      <div className="text-2xl font-bold text-white mt-1">{value}</div>
      {children && <div className="mt-2">{children}</div>}
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function SystemMonitorPage() {
  const [res, setRes] = useState<Resources | null>(null);
  const [tasks, setTasks] = useState<TasksSnapshot | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [r, t] = await Promise.all([apiClient.system.resources(), apiClient.tasks.list()]);
    setRes(r.data);
    setTasks(t.data);
  }, []);

  useSmartPoll(refresh, { intervalMs: 3000, stopAfterHiddenMs: 60_000 });

  const act = async (fn: () => Promise<unknown>, key: string) => {
    setBusy(key);
    try { await fn(); await refresh(); } finally { setBusy(null); }
  };

  const host = res?.host;
  const lag = res?.loop_lag;
  const grouped: Record<string, TaskInfo[]> = {};
  (tasks?.tasks || []).forEach((t) => {
    (grouped[t.category] ||= []).push(t);
  });

  const lagColor = (lag?.p95_ms ?? 0) < 50 ? 'text-green-400'
    : (lag?.p95_ms ?? 0) < 200 ? 'text-yellow-400' : 'text-red-400';

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Activity className="w-6 h-6 text-tradebot-accent" />
          <div>
            <h1 className="text-xl font-bold text-white">System Monitor</h1>
            <p className="text-sm text-gray-400">
              Tier <span className="text-tradebot-accent font-semibold uppercase">{res?.tier || '—'}</span>
              {res?.profile ? ` · profile ${res.profile}` : ''} · {tasks?.task_count ?? 0} background tasks
            </p>
          </div>
        </div>
        <button onClick={() => act(refresh, 'refresh')}
          className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 transition" title="Refresh">
          <RefreshCw className={`w-4 h-4 ${busy === 'refresh' ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Paused banner */}
      {tasks && tasks.paused_count > 0 && (
        <div className="bg-yellow-900/30 border border-yellow-700 rounded-lg p-3 text-sm text-yellow-200">
          <strong>{tasks.paused_count}</strong> task(s) paused:{' '}
          {tasks.paused.map((p) => `${p.id} (${p.paused_by})`).join(', ')}
        </div>
      )}

      {/* Stat strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard icon={<Cpu className="w-3.5 h-3.5" />} label="Host CPU"
          value={`${host?.cpu_percent?.toFixed(0) ?? '—'}%`}>
          <Bar pct={host?.cpu_percent ?? 0} color="bg-blue-500" />
        </StatCard>
        <StatCard icon={<MemoryStick className="w-3.5 h-3.5" />} label="Host RAM"
          value={`${host?.mem_percent?.toFixed(0) ?? '—'}%`}>
          <Bar pct={host?.mem_percent ?? 0} color={(host?.mem_percent ?? 0) > 85 ? 'bg-red-500' : 'bg-emerald-500'} />
          <div className="mt-1">
            <div className="text-[10px] text-gray-500 mb-0.5">swap {host?.swap_percent?.toFixed(0) ?? 0}%</div>
            <Bar pct={host?.swap_percent ?? 0} color="bg-red-600" />
          </div>
        </StatCard>
        <StatCard icon={<Gauge className="w-3.5 h-3.5" />} label="Backend RSS"
          value={fmtBytes(res?.process?.rss)} />
        <StatCard icon={<Activity className="w-3.5 h-3.5" />} label="Loop lag p95"
          value={<span className={lagColor}>{lag?.available ? `${lag.p95_ms} ms` : '—'}</span>}>
          <div className="text-[10px] text-gray-500">
            p50 {lag?.p50_ms ?? '—'}ms · max {lag?.max_ms ?? '—'}ms
            {res?.offload && ` · offload q${res.offload.heavy_queue_depth ?? 0}`}
          </div>
        </StatCard>
      </div>

      {/* Presets */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-gray-400 mr-1">Presets:</span>
        <button onClick={() => act(() => apiClient.tasks.preset('battery_saver'), 'p1')}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-800/60 border border-gray-700 text-gray-200 hover:bg-gray-700 transition">
          <BatteryLow className="w-4 h-4" /> Battery saver
        </button>
        <button onClick={() => act(() => apiClient.tasks.preset('balanced'), 'p2')}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-800/60 border border-gray-700 text-gray-200 hover:bg-gray-700 transition">
          <Scale className="w-4 h-4" /> Balanced
        </button>
        <button onClick={() => act(() => apiClient.tasks.preset('full_power'), 'p3')}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-gray-800/60 border border-gray-700 text-gray-200 hover:bg-gray-700 transition">
          <Zap className="w-4 h-4" /> Full power
        </button>
      </div>

      {/* Tasks grouped by category */}
      <div className="space-y-6">
        {Object.entries(grouped).sort().map(([category, items]) => (
          <div key={category} className="bg-gray-800/30 border border-gray-700 rounded-lg overflow-hidden">
            <div className="px-4 py-2 bg-gray-800/50 text-xs uppercase tracking-wide text-gray-400 font-semibold">
              {category}
            </div>
            <table className="w-full text-sm">
              <thead className="text-gray-500 text-xs">
                <tr className="border-b border-gray-700">
                  <th className="text-left px-4 py-2 font-medium">Task</th>
                  <th className="text-left px-2 py-2 font-medium">State</th>
                  <th className="text-right px-2 py-2 font-medium">Interval</th>
                  <th className="text-right px-2 py-2 font-medium">Cycles</th>
                  <th className="text-right px-2 py-2 font-medium">CPU</th>
                  <th className="text-right px-4 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.id} className="border-b border-gray-800/60 last:border-0">
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-1.5 text-white">
                        {t.critical && <Lock className="w-3 h-3 text-amber-400" />}
                        {t.name}
                      </div>
                      <div className="text-[10px] text-gray-500">{t.id}</div>
                    </td>
                    <td className="px-2 py-2">
                      {t.paused ? (
                        <span className="text-yellow-400">paused ({t.paused_by})</span>
                      ) : t.running === false ? (
                        <span className="text-gray-500">stopped</span>
                      ) : (
                        <span className="text-green-400">running</span>
                      )}
                      {t.error_count > 0 && (
                        <div className="text-[10px] text-red-400" title={t.last_error || ''}>
                          {t.error_count} err
                        </div>
                      )}
                    </td>
                    <td className="px-2 py-2 text-right text-gray-300">{t.interval_seconds}s</td>
                    <td className="px-2 py-2 text-right text-gray-400">{t.cycle_count}</td>
                    <td className="px-2 py-2 text-right text-gray-400">{Math.round(t.cumulative_cpu_ms)}ms</td>
                    <td className="px-4 py-2">
                      <div className="flex items-center justify-end gap-1">
                        {t.paused ? (
                          <IconBtn title="Resume" onClick={() => act(() => apiClient.tasks.resume(t.id), t.id)}>
                            <Play className="w-3.5 h-3.5" />
                          </IconBtn>
                        ) : (
                          <IconBtn title={t.critical ? 'Pause (critical — confirm)' : 'Pause'}
                            onClick={() => {
                              if (t.critical && !window.confirm(`${t.name} is critical. Force-pause?`)) return;
                              act(() => apiClient.tasks.pause(t.id, t.critical), t.id);
                            }}>
                            <Pause className="w-3.5 h-3.5" />
                          </IconBtn>
                        )}
                        <IconBtn title="Stop" onClick={() => {
                          if (t.critical && !window.confirm(`${t.name} is critical. Stop it?`)) return;
                          act(() => apiClient.tasks.stop(t.id), t.id);
                        }}>
                          <Square className="w-3.5 h-3.5" />
                        </IconBtn>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </div>
  );
}

function IconBtn({ title, onClick, children }: {
  title: string; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button title={title} onClick={onClick}
      className="p-1.5 rounded text-gray-400 hover:text-white hover:bg-gray-700 transition">
      {children}
    </button>
  );
}
